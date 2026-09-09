"""Streamlit Community Cloud entrypoint retaining the Gallery03 frontend."""
import shutil
import tempfile
from pathlib import Path
import streamlit as st
import streamlit.components.v1 as components
from cloud_service import handle_request

ROOT = Path(__file__).resolve().parent
st.set_page_config(page_title='UT-checker', layout='wide', initial_sidebar_state='collapsed')


@st.cache_resource
def frontend_directory():
    # Serve only public UI assets, never the repository or backend source.
    folder = Path(tempfile.mkdtemp(prefix='ut-checker-ui-'))
    for name in ('assets', 'css', 'js'):
        shutil.copytree(ROOT / name, folder / name)
    for name in ('app.js', 'forms.js'):
        shutil.copy2(ROOT / name, folder / name)
    html = (ROOT / 'index.html').read_text(encoding='utf-8')
    html = html.replace('<script src="forms.js">', '<script src="js/cloud-transport.js"></script>\n <script src="forms.js">')
    (folder / 'index.html').write_text(html, encoding='utf-8')
    return str(folder)


st.markdown('''<style>
header[data-testid="stHeader"], [data-testid="stToolbar"] {display:none;}
.stMainBlockContainer {padding:0!important;max-width:none!important;}
[data-testid="stVerticalBlock"] {gap:0;}
iframe[title="ut_checker_ui"] {width:100%;height:100dvh!important;display:block;border:0;}
.stMain {overflow:hidden;}
</style>''', unsafe_allow_html=True)
component = components.declare_component('ut_checker_ui', path=frontend_directory())
event = component(response=st.session_state.get('ut_response'), key='ut_ui', default=None)
if isinstance(event, dict):
    if event.get('kind') in ('ack', 'clear'):
        if st.session_state.pop('ut_response', None) is not None:
            st.rerun()
    elif event.get('kind') == 'request' and event.get('id') != st.session_state.get('ut_last_id'):
        st.session_state.ut_last_id = event.get('id')
        st.session_state.ut_response = handle_request(event)
        st.rerun()
