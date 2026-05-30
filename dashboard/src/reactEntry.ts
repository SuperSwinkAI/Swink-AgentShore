export function getOrCreateReactRoot(): HTMLDivElement {
  document.body.classList.add("dashboard-active");
  const existing = document.getElementById("react-root");
  if (existing instanceof HTMLDivElement) {
    return existing;
  }

  const root = document.createElement("div");
  root.id = "react-root";
  document.body.appendChild(root);
  return root;
}
