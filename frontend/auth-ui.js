const passwordIcons = {
  show: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M2.5 12s3.4-6.2 9.5-6.2 9.5 6.2 9.5 6.2-3.4 6.2-9.5 6.2S2.5 12 2.5 12Z"/><circle cx="12" cy="12" r="3"/></svg>',
  hide: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M3 3l18 18M10.6 10.6a2 2 0 0 0 2.8 2.8M9.9 5.9A10.5 10.5 0 0 1 12 5.7c6.1 0 9.5 6.3 9.5 6.3a15 15 0 0 1-3.1 3.7M6.1 6.1C3.8 7.7 2.5 12 2.5 12s3.4 6.3 9.5 6.3c1.3 0 2.4-.3 3.4-.7"/></svg>',
};

document.querySelectorAll('input[type="password"]').forEach((input) => {
  const field = document.createElement("div");
  field.className = "password-field";
  input.before(field);
  field.append(input);

  const toggle = document.createElement("button");
  toggle.className = "password-toggle";
  toggle.type = "button";
  toggle.setAttribute("aria-label", "Show password");
  toggle.innerHTML = passwordIcons.show;
  toggle.addEventListener("click", () => {
    const reveal = input.type === "password";
    input.type = reveal ? "text" : "password";
    toggle.setAttribute("aria-label", reveal ? "Hide password" : "Show password");
    toggle.innerHTML = passwordIcons[reveal ? "hide" : "show"];
  });
  field.append(toggle);
});
