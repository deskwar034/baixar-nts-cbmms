#!/usr/bin/env python3
from __future__ import annotations

import html
import io
import re
import time
import unicodedata
import zipfile
from dataclasses import dataclass
from typing import List

import requests
import streamlit as st
from bs4 import BeautifulSoup

DEFAULT_URL = "https://sistemas.bombeiros.ms.gov.br/arquivos/dat/normas-tecnicas.xhtml"
TIMEOUT = 90


@dataclass
class NTItem:
    name: str
    form_id: str
    view_state: str
    field_name: str
    field_value: str


def normalize_space(text: str) -> str:
    return " ".join(text.split())


def safe_filename(name: str, keep_accents: bool = True) -> str:
    name = html.unescape(normalize_space(name)).strip()
    invalid = '<>:"/\\|?*\0'
    for ch in invalid:
        name = name.replace(ch, "-")
    name = name.rstrip(" .")
    if not keep_accents:
        name = "".join(
            c for c in unicodedata.normalize("NFKD", name)
            if not unicodedata.combining(c)
        )
    return name or "arquivo"


def build_session() -> requests.Session:
    session = requests.Session()
    session.headers.update(
        {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/147.0.0.0 Safari/537.36"
            )
        }
    )
    return session


def fetch_listing(session: requests.Session, url: str) -> str:
    response = session.get(url, timeout=TIMEOUT)
    response.raise_for_status()
    response.encoding = response.encoding or "utf-8"
    return response.text


def extract_links(page_html: str) -> List[NTItem]:
    soup = BeautifulSoup(page_html, "html.parser")

    items: List[NTItem] = []

    # Percorre todos os links das NT e descobre o form correto a partir do onclick
    for a in soup.select("a.btn-link"):
        raw_name = normalize_space(a.get_text(" ", strip=True))
        if not raw_name.startswith("NT "):
            continue

        onclick = a.get("onclick", "")

        # Ex.: mojarra.jsfcljs(document.getElementById('j_idt15'),{'j_idt15:j_idt17:0:j_idt19':'j_idt15:j_idt17:0:j_idt19'},'_blank')
        form_match = re.search(r"getElementById\('([^']+)'\)", onclick)
        field_match = re.search(r"\{'([^']+)':'([^']+)'\}", onclick)

        if not form_match or not field_match:
            continue

        form_id = form_match.group(1)
        form = soup.find("form", id=form_id)
        if not form:
            continue

        view_state_input = form.find("input", attrs={"name": "javax.faces.ViewState"})
        if not view_state_input:
            # fallback global, caso o campo esteja fora do form no HTML salvo
            view_state_input = soup.find("input", attrs={"name": "javax.faces.ViewState"})

        if not view_state_input or not view_state_input.get("value"):
            raise RuntimeError(f"Não encontrei o javax.faces.ViewState para o form {form_id}.")

        items.append(
            NTItem(
                name=raw_name,
                form_id=form_id,
                view_state=view_state_input["value"],
                field_name=field_match.group(1),
                field_value=field_match.group(2),
            )
        )

    if not items:
        raise RuntimeError("Nenhuma NT foi encontrada na página.")

    return items


def download_pdf_bytes(session: requests.Session, url: str, item: NTItem) -> bytes:
    origin_match = re.match(r"^https?://[^/]+", url)
    origin = origin_match.group(0) if origin_match else ""

    payload = {
        item.form_id: item.form_id,
        "javax.faces.ViewState": item.view_state,
        item.field_name: item.field_value,
    }

    headers = {
        "Referer": url,
        "Origin": origin,
        "Content-Type": "application/x-www-form-urlencoded",
    }

    response = session.post(url, data=payload, headers=headers, timeout=TIMEOUT)
    response.raise_for_status()

    content_type = (response.headers.get("Content-Type") or "").lower()
    content = response.content

    if "pdf" not in content_type and not content.startswith(b"%PDF"):
        snippet = ""
        try:
            snippet = response.text[:300].replace("\n", " ")
        except Exception:
            pass
        raise RuntimeError(
            f"Resposta inesperada para '{item.name}'. "
            f"Content-Type={content_type!r}. Trecho={snippet!r}"
        )

    return content


def generate_zip(
    keep_accents: bool = True,
    delay_seconds: float = 0.2,
    status_callback=None,
) -> tuple[bytes, List[str]]:
    session = build_session()
    page_html = fetch_listing(session, DEFAULT_URL)
    items = extract_links(page_html)

    zip_buffer = io.BytesIO()
    saved_files: List[str] = []

    with zipfile.ZipFile(zip_buffer, mode="w", compression=zipfile.ZIP_DEFLATED) as zf:
        for index, item in enumerate(items, start=1):
            pdf_bytes = download_pdf_bytes(session, DEFAULT_URL, item)
            filename = safe_filename(item.name, keep_accents=keep_accents) + ".pdf"
            zf.writestr(filename, pdf_bytes)
            saved_files.append(filename)

            if status_callback:
                status_callback(index, len(items), filename)

            if delay_seconds > 0:
                time.sleep(delay_seconds)

    zip_buffer.seek(0)
    return zip_buffer.getvalue(), saved_files


st.set_page_config(page_title="Baixar Normas Técnicas", page_icon="📄", layout="centered")
st.title("📄 Download das Normas Técnicas em ZIP")
st.caption("Baixa automaticamente as Normas Técnicas do CBMMS e gera um único arquivo .zip contendo apenas PDFs.")

with st.expander("Configurações", expanded=True):
    keep_accents = st.checkbox("Manter acentos nos nomes dos arquivos", value=True)
    delay_seconds = st.slider("Intervalo entre downloads (segundos)", min_value=0.0, max_value=2.0, value=0.2, step=0.1)

st.markdown(f"**Fonte:** `{DEFAULT_URL}`")

if "zip_bytes" not in st.session_state:
    st.session_state.zip_bytes = None
if "saved_files" not in st.session_state:
    st.session_state.saved_files = []

start = st.button("Baixar normas e gerar ZIP", type="primary", use_container_width=True)

if start:
    progress = st.progress(0, text="Iniciando...")
    status_box = st.empty()

    def update_status(current: int, total: int, filename: str) -> None:
        progress.progress(current / total, text=f"Baixando {current}/{total}")
        status_box.info(f"Último arquivo adicionado: {filename}")

    try:
        zip_bytes, saved_files = generate_zip(
            keep_accents=keep_accents,
            delay_seconds=delay_seconds,
            status_callback=update_status,
        )
        st.session_state.zip_bytes = zip_bytes
        st.session_state.saved_files = saved_files
        progress.progress(1.0, text="Concluído")
        status_box.success(f"ZIP gerado com sucesso com {len(saved_files)} PDFs.")
    except Exception as exc:
        progress.empty()
        status_box.error(f"Erro ao gerar o ZIP: {exc}")

if st.session_state.zip_bytes:
    st.download_button(
        label="📦 Baixar arquivo ZIP",
        data=st.session_state.zip_bytes,
        file_name="normas_tecnicas_cbmms.zip",
        mime="application/zip",
        use_container_width=True,
    )

    with st.expander(f"Arquivos no ZIP ({len(st.session_state.saved_files)})", expanded=False):
        for name in st.session_state.saved_files:
            st.write(f"- {name}")
