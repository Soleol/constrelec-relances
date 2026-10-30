"""Conserver le défilement de l'onglet après une action sur une carte client."""
import json
import secrets
import streamlit as st
import streamlit.components.v1 as composants


SCRIPT_DEFILEMENT = r"""
(() => {
  if (window.__constrelecDefilementV88) return;
  const state = {saved: null, token: null, observer: null, timer: null, frame: null, active: false};
  const scroller = () => document.querySelector('[data-testid="stMain"]')
    || document.querySelector('section.main') || document.scrollingElement;
  const stop = () => {
    state.active = false;
    if (state.observer) state.observer.disconnect();
    if (state.timer) window.clearTimeout(state.timer);
    if (state.frame) window.cancelAnimationFrame(state.frame);
    state.observer = state.timer = state.frame = null;
  };
  document.addEventListener('click', event => {
    const button = event.target.closest && event.target.closest('button');
    if (!button || button.disabled) return;
    const action = button.closest('[class*="st-key-fait_"], [class*="st-key-attendre_"], [class*="st-key-perdre_"], [class*="st-key-reprendre_action_"]');
    if (action) {
      stop();
      const element = scroller();
      state.saved = element ? element.scrollTop : window.scrollY;
    } else if (!button.closest('[role="dialog"]')) {
      state.saved = null;
    }
  }, true);
  // L'utilisateur garde toujours la main s'il reprend lui-même le défilement.
  for (const event of ['wheel', 'touchmove', 'pointerdown']) {
    document.addEventListener(event, () => { if (state.active) stop(); }, {capture: true, passive: true});
  }
  document.addEventListener('keydown', event => {
    if (state.active && ['ArrowUp', 'ArrowDown', 'PageUp', 'PageDown', 'Home', 'End', ' '].includes(event.key)) stop();
  }, true);
  const restore = token => {
    if (!token || token === state.token) return;
    state.token = token;
    if (state.saved === null) return;
    const target = state.saved;
    state.saved = null;
    stop();
    state.active = true;
    const update = () => {
      state.frame = null;
      if (!state.active || state.token !== token || document.querySelector('[role="dialog"]')) return;
      const element = scroller();
      if (!element) return;
      // La hauteur peut changer pendant le recalcul ou après retrait de la carte.
      element.scrollTop = Math.min(target, Math.max(0, element.scrollHeight - element.clientHeight));
    };
    const schedule = () => {
      if (state.active && state.token === token && state.frame === null) state.frame = window.requestAnimationFrame(update);
    };
    state.observer = new MutationObserver(schedule);
    state.observer.observe(document.body, {childList: true, subtree: true, attributes: true});
    const tick = () => {
      if (!state.active || state.token !== token) return;
      update();
      window.setTimeout(tick, 60);
    };
    tick();
    state.timer = window.setTimeout(stop, 3500);
  };
  window.__constrelecDefilementV88 = {restore};
})();
"""


def installer_defilement():
    reprise = st.session_state.pop('_reprendre_defilement', None)
    # Script constant écrit par l'application ; aucune donnée client ni jeton.
    code = "<script>const doc=window.parent.document;const script=doc.createElement('script');"
    code += 'script.textContent=' + json.dumps(SCRIPT_DEFILEMENT) + ';doc.body.appendChild(script);script.remove();'
    code += 'window.parent.__constrelecDefilementV88.restore(' + json.dumps(reprise) + ');</script>'
    with st.container(key='memo_defilement'):
        st.markdown('<style>.st-key-memo_defilement{display:none}</style>', unsafe_allow_html=True)
        composants.html(code, height=0, tab_index=-1)


def actualiser_sans_deplacer():
    st.session_state['_reprendre_defilement'] = secrets.token_hex(12)
    st.rerun()
