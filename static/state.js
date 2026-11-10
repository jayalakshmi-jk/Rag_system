export const state = {
  me: null,
  documents: [],
  selected: new Set(),
  conversations: [],
  activeId: null,
  streaming: false,
};
export const bus = new EventTarget();
export const emit = (name, detail) => bus.dispatchEvent(new CustomEvent(name, { detail }));
export const on = (name, handler) => bus.addEventListener(name, (event) => handler(event.detail));

export const readyDocuments = () => state.documents.filter((d) => d.status === "ready");

export function setQuota(quota) {
  if (!quota || !state.me) return;
  state.me.quota = { ...state.me.quota, ...quota };
  emit("quota");
}
