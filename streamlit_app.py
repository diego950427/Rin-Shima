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
def frontend_directory(revision):
    # Serve only public UI assets, never the repository or backend source.
    folder = Path(tempfile.mkdtemp(prefix='ut-checker-ui-'))
    for name in ('assets', 'css', 'js'):
        shutil.copytree(ROOT / name, folder / name)
    for name in ('app.js', 'forms.js'):
        shutil.copy2(ROOT / name, folder / name)
    html = (ROOT / 'index.html').read_text(encoding='utf-8')
    # Components v1 disables iframe viewport scrolling. Use an explicit element
    # scroller so wheel, touch, keyboard and sticky illustration work inside it.
    html = html.replace('</head>', '''<style>
html, body {height:100%;overflow:hidden;}
.cloud-scroll {height:100dvh;overflow-y:auto;overflow-x:hidden;scroll-behavior:smooth;}
@media(prefers-reduced-motion:reduce) {.cloud-scroll {scroll-behavior:auto;}}
</style></head>''')
    html = html.replace('<body>', '<body><div class="cloud-scroll" tabindex="0" aria-label="學分規劃內容">')
    html = html.replace('</body>', '</div></body>')
    html = html.replace('<script src="forms.js">', '<script src="js/cloud-transport.js"></script>\n <script src="forms.js">')
    (folder / 'index.html').write_text(html, encoding='utf-8')
    return str(folder)


st.markdown('''<style>
header[data-testid="stHeader"], [data-testid="stToolbar"] {display:none;}
.stMainBlockContainer {padding:0!important;max-width:none!important;}
[data-testid="stVerticalBlock"] {gap:0;}
iframe[title$="ut_checker_ui"] {width:100%;height:100dvh!important;display:block;border:0;}
.stMain {overflow:hidden;}
</style>''', unsafe_allow_html=True)
# Invalidate packaged UI when tracked frontend files change during hot reload.
ui_files = [ROOT / 'index.html', ROOT / 'forms.js', ROOT / 'app.js']
ui_files += [p for folder in ('assets', 'css', 'js') for p in (ROOT / folder).rglob('*') if p.is_file()]
ui_revision = tuple((str(p.relative_to(ROOT)), p.stat().st_mtime_ns, p.stat().st_size) for p in sorted(ui_files))
component = components.declare_component('ut_checker_ui', path=frontend_directory(ui_revision))
event = component(response=st.session_state.get('ut_response'), key='ut_ui', default=None)
if isinstance(event, dict):
    if event.get('kind') in ('ack', 'clear'):
        if st.session_state.pop('ut_response', None) is not None:
            st.rerun()
    elif event.get('kind') == 'request' and event.get('id') != st.session_state.get('ut_last_id'):
        st.session_state.ut_last_id = event.get('id')
        st.session_state.ut_response = handle_request(event)
        st.rerun()
