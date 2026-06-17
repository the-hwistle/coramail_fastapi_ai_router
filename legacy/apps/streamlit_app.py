from __future__ import annotations

import os
from pathlib import Path

import streamlit as st
import streamlit.components.v1 as components


APP_DIR = Path(__file__).resolve().parent
PROJECT_DIR = APP_DIR.parents[1]
INDEX_HTML = PROJECT_DIR / "static" / "index.html"
DEFAULT_API_BASE_URL = os.getenv("CORAMAIL_API_BASE_URL", "http://127.0.0.1:8011")


def load_shell(api_base_url: str) -> str:
    html = INDEX_HTML.read_text(encoding="utf-8")
    api_script = f"""
  <script>
    window.CORAMAIL_API_BASE_URL = {api_base_url!r};
  </script>
"""
    return html.replace("</head>", f"{api_script}</head>", 1)


st.set_page_config(
    page_title="CoRA Mail AI Router",
    page_icon="✉️",
    layout="wide",
    initial_sidebar_state="collapsed",
)

st.markdown(
    """
    <style>
      .stApp { background: #fbf9fb; }
      header[data-testid="stHeader"],
      div[data-testid="stToolbar"],
      div[data-testid="stDecoration"],
      div[data-testid="stStatusWidget"] { display: none !important; }
      .block-container {
        padding: 0 !important;
        max-width: none !important;
      }
      iframe {
        display: block;
        width: 100%;
        border: 0;
      }
    </style>
    """,
    unsafe_allow_html=True,
)

components.html(
    load_shell(DEFAULT_API_BASE_URL),
    height=980,
    scrolling=True,
)
