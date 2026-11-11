// Stand-ins for the few Home Assistant elements the cards use, so a card can be shown on a plain
// page (preview/index.html). Not part of the dashboard bundle.

class HaCard extends HTMLElement {
  constructor() {
    super();
    this.attachShadow({ mode: "open" }).innerHTML = `
      <style>
        :host {
          display: block;
          background: var(--ha-card-background, var(--card-background-color));
          border: 1px solid var(--ha-card-border-color, var(--divider-color));
          border-radius: var(--ha-card-border-radius, 12px);
          color: var(--primary-text-color);
          box-sizing: border-box;
        }
      </style>
      <slot></slot>`;
  }
}
customElements.define("ha-card", HaCard);

// Icons: "mdi:name" from the Material Design Icons font, "cellkeeper:name" from the dashboard's
// own icon set (it registers itself in window.customIcons, as it does in Home Assistant).
class HaIcon extends HTMLElement {
  static observedAttributes = ["icon"];
  constructor() {
    super();
    this.attachShadow({ mode: "open" });
  }
  connectedCallback() {
    this.draw();
  }
  attributeChangedCallback() {
    this.draw();
  }
  set icon(value) {
    this.setAttribute("icon", value ?? "");
  }
  get icon() {
    return this.getAttribute("icon") ?? "";
  }
  async draw() {
    const [set, name] = this.icon.split(":");
    const style = `<style>
      @import url("https://cdn.jsdelivr.net/npm/@mdi/font@7.4.47/css/materialdesignicons.min.css");
      :host { display: inline-flex; width: var(--mdc-icon-size, 24px); height: var(--mdc-icon-size, 24px);
              align-items: center; justify-content: center; vertical-align: middle; }
      .mdi { font-size: var(--mdc-icon-size, 24px); line-height: 1; }
      svg { width: 100%; height: 100%; fill: currentColor; }
    </style>`;
    if (set === "mdi" && name) {
      this.shadowRoot.innerHTML = `${style}<span class="mdi mdi-${name}"></span>`;
      return;
    }
    const custom = window.customIcons?.[set];
    if (custom && name) {
      const icon = await custom.getIcon(name);
      if (icon?.path) {
        this.shadowRoot.innerHTML = `${style}<svg viewBox="${icon.viewBox ?? "0 0 24 24"}"><path d="${icon.path}"></path></svg>`;
        return;
      }
    }
    this.shadowRoot.innerHTML = style;
  }
}
customElements.define("ha-icon", HaIcon);
