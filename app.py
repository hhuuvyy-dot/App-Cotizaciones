from io import BytesIO
import re
from typing import Any, Dict, List, Optional, Tuple

import pandas as pd
import pdfplumber
import streamlit as st


st.set_page_config(
    page_title="Dashboard de Cotizaciones PDF",
    layout="wide",
)


# La regex del cliente busca "AT'N:" en la cabecera y toma el resto de la linea.
# Se permiten pequenas variantes del apostrofo para tolerar diferencias en los PDFs.
CLIENT_REGEX = re.compile(
    r"AT\s*['`\u2019\u00B4]?\s*N\s*:\s*(?P<client>[^\n\r]+)",
    re.IGNORECASE,
)

# La regex del producto asume el formato del ejemplo:
# - clave/material al inicio (C38, S24, CTX, etc.)
# - texto libre en medio (descripcion)
# - color al final, justo antes de "KG $"
# - precio numerico al cierre
# Asi ignoramos de forma explicita el texto irrelevante "KG $".
PRODUCT_REGEX = re.compile(
    r"""
    ^\s*
    (?P<material>[A-Z0-9][A-Z0-9\-]*)
    .*?
    \s+(?P<color>[A-Z\u00C1\u00C9\u00CD\u00D3\u00DA\u00DC\u00D10-9/.\-]+(?:\s+[A-Z\u00C1\u00C9\u00CD\u00D3\u00DA\u00DC\u00D10-9/.\-]+){0,2})
    \s+KG\s*\$\s*(?P<price>\d[\d.,]*)
    \s*$
    """,
    re.IGNORECASE | re.VERBOSE,
)

NON_COLOR_PREFIXES = {
    "UV",
    "CON",
    "TIPO",
}


def normalize_spaces(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def extract_client_name(text: str) -> str:
    match = CLIENT_REGEX.search(text)
    if not match:
        return "Cliente no encontrado"
    return normalize_spaces(match.group("client"))


def normalize_color(raw_color: str) -> str:
    color = normalize_spaces(raw_color).upper()
    tokens = color.split()

    while len(tokens) > 1 and tokens[0] in NON_COLOR_PREFIXES:
        tokens = tokens[1:]

    return " ".join(tokens)


def parse_price(raw_price: str) -> float:
    value = raw_price.strip()

    if "," in value and "." in value:
        if value.rfind(",") > value.rfind("."):
            value = value.replace(".", "").replace(",", ".")
        else:
            value = value.replace(",", "")
    elif "," in value:
        if re.fullmatch(r"\d+,\d{2}", value):
            value = value.replace(",", ".")
        else:
            value = value.replace(",", "")

    return float(value)


def parse_product_line(line: str, client_name: str, file_name: str) -> Optional[Dict[str, Any]]:
    cleaned_line = normalize_spaces(line)
    match = PRODUCT_REGEX.match(cleaned_line)

    if not match:
        return None

    price = parse_price(match.group("price"))
    color = normalize_color(match.group("color"))
    material = match.group("material").upper()

    return {
        "Archivo": file_name,
        "Cliente": client_name,
        "Material": material,
        "Color": color,
        "Precio": price,
        "Linea original": cleaned_line,
    }


def extract_text_lines_from_pdf(file_bytes: bytes) -> Tuple[List[str], str]:
    all_lines: List[str] = []

    with pdfplumber.open(BytesIO(file_bytes)) as pdf:
        for page in pdf.pages:
            page_text = page.extract_text(x_tolerance=2, y_tolerance=3) or ""
            page_lines = [
                normalize_spaces(line)
                for line in page_text.splitlines()
                if normalize_spaces(line)
            ]
            all_lines.extend(page_lines)

    return all_lines, "\n".join(all_lines)


def process_uploaded_pdf(uploaded_file) -> Tuple[pd.DataFrame, List[str]]:
    file_bytes = uploaded_file.getvalue()
    lines, full_text = extract_text_lines_from_pdf(file_bytes)
    client_name = extract_client_name(full_text)

    records: List[Dict[str, Any]] = []
    skipped_lines: List[str] = []

    for line in lines:
        if "KG" not in line.upper() or "$" not in line:
            continue

        record = parse_product_line(line, client_name, uploaded_file.name)
        if record:
            records.append(record)
        else:
            skipped_lines.append(line)

    df = pd.DataFrame(records)
    if not df.empty:
        df = df.sort_values(by=["Cliente", "Material", "Color"]).reset_index(drop=True)

    return df, skipped_lines


def build_excel_file(dataframe: pd.DataFrame) -> bytes:
    output = BytesIO()

    with pd.ExcelWriter(output, engine="openpyxl") as writer:
        dataframe.to_excel(writer, index=False, sheet_name="Cotizaciones")
        worksheet = writer.sheets["Cotizaciones"]

        for column_cells in worksheet.columns:
            values = [str(cell.value) if cell.value is not None else "" for cell in column_cells]
            max_length = max(len(value) for value in values) if values else 0
            worksheet.column_dimensions[column_cells[0].column_letter].width = min(max_length + 2, 45)

    output.seek(0)
    return output.getvalue()


def apply_filters(dataframe: pd.DataFrame) -> pd.DataFrame:
    filtered = dataframe.copy()

    st.sidebar.header("Filtros")

    files = st.sidebar.multiselect(
        "Archivo",
        options=sorted(filtered["Archivo"].dropna().unique().tolist()),
    )
    clients = st.sidebar.multiselect(
        "Cliente",
        options=sorted(filtered["Cliente"].dropna().unique().tolist()),
    )
    materials = st.sidebar.multiselect(
        "Material",
        options=sorted(filtered["Material"].dropna().unique().tolist()),
    )
    colors = st.sidebar.multiselect(
        "Color",
        options=sorted(filtered["Color"].dropna().unique().tolist()),
    )

    if files:
        filtered = filtered[filtered["Archivo"].isin(files)]
    if clients:
        filtered = filtered[filtered["Cliente"].isin(clients)]
    if materials:
        filtered = filtered[filtered["Material"].isin(materials)]
    if colors:
        filtered = filtered[filtered["Color"].isin(colors)]

    return filtered.reset_index(drop=True)


def render_metrics(dataframe: pd.DataFrame) -> None:
    total_files = dataframe["Archivo"].nunique()
    total_products = len(dataframe)
    total_clients = dataframe["Cliente"].nunique()
    avg_price = dataframe["Precio"].mean()

    col1, col2, col3, col4 = st.columns(4)
    col1.metric("Archivos procesados", total_files)
    col2.metric("Productos extraidos", total_products)
    col3.metric("Clientes detectados", total_clients)
    col4.metric("Precio promedio", f"${avg_price:,.2f}")


def render_charts(dataframe: pd.DataFrame) -> None:
    if dataframe.empty:
        return

    left_col, right_col = st.columns(2)

    with left_col:
        st.subheader("Precio promedio por material")
        by_material = (
            dataframe.groupby("Material", as_index=False)["Precio"]
            .mean()
            .sort_values("Precio", ascending=False)
        )
        st.bar_chart(by_material, x="Material", y="Precio", use_container_width=True)

    with right_col:
        st.subheader("Conteo de productos por color")
        by_color = (
            dataframe.groupby("Color", as_index=False)
            .size()
            .rename(columns={"size": "Cantidad"})
            .sort_values("Cantidad", ascending=False)
        )
        st.bar_chart(by_color, x="Color", y="Cantidad", use_container_width=True)


def main() -> None:
    st.title("Extraccion de cotizaciones PDF")
    st.caption(
        "Carga uno o varios PDFs de cotizaciones para extraer cliente, material, color y precio."
    )

    with st.expander("Formato esperado", expanded=False):
        st.markdown(
            """
            Ejemplos de lineas compatibles:

            - `C38 (TR COMPACTO) UV COLORES KG $ 53.60`
            - `S24 (TR TRASLUCIDO BRILLO) NATURAL KG $ 62.27`

            La app busca el nombre del cliente a partir de `AT'N:` y el color justo antes de `KG $`.
            """
        )

    if "df_resultado" not in st.session_state:
        st.session_state["df_resultado"] = pd.DataFrame()
    if "warnings" not in st.session_state:
        st.session_state["warnings"] = []

    uploaded_files = st.file_uploader(
        "Sube uno o varios archivos PDF",
        type=["pdf"],
        accept_multiple_files=True,
    )

    if st.button("Procesar Cotizaciones", type="primary", use_container_width=True):
        if not uploaded_files:
            st.warning("Primero selecciona al menos un archivo PDF.")
        else:
            processed_frames: List[pd.DataFrame] = []
            warnings: List[str] = []

            with st.spinner("Procesando cotizaciones..."):
                for uploaded_file in uploaded_files:
                    try:
                        df_file, skipped_lines = process_uploaded_pdf(uploaded_file)

                        if df_file.empty:
                            warnings.append(
                                f"No se detectaron productos validos en `{uploaded_file.name}`."
                            )
                        else:
                            processed_frames.append(df_file)

                        if skipped_lines:
                            warnings.append(
                                f"`{uploaded_file.name}` tuvo {len(skipped_lines)} linea(s) con `KG $` que no coincidieron con el patron esperado."
                            )
                    except Exception as exc:
                        warnings.append(
                            f"Error procesando `{uploaded_file.name}`: {exc}"
                        )

            if processed_frames:
                combined_df = pd.concat(processed_frames, ignore_index=True)
                combined_df["Precio"] = pd.to_numeric(combined_df["Precio"], errors="coerce")
                st.session_state["df_resultado"] = combined_df.dropna(subset=["Precio"]).reset_index(drop=True)
                st.success("Extraccion completada correctamente.")
            else:
                st.session_state["df_resultado"] = pd.DataFrame()
                st.error("No fue posible extraer registros con el patron esperado.")

            st.session_state["warnings"] = warnings

    df_resultado = st.session_state["df_resultado"]

    if st.session_state["warnings"]:
        with st.expander("Observaciones del procesamiento", expanded=False):
            for warning in st.session_state["warnings"]:
                st.write(f"- {warning}")

    if df_resultado.empty:
        st.info("Cuando proceses PDFs, aqui veras el dashboard y la tabla consolidada.")
        return

    filtered_df = apply_filters(df_resultado)
    render_metrics(filtered_df)
    st.divider()
    render_charts(filtered_df)

    st.subheader("Datos extraidos")
    st.dataframe(filtered_df, use_container_width=True, hide_index=True)

    excel_bytes = build_excel_file(df_resultado)
    st.download_button(
        label="Exportar DataFrame completo a Excel",
        data=excel_bytes,
        file_name="cotizaciones_extraidas.xlsx",
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        use_container_width=True,
    )


if __name__ == "__main__":
    main()
