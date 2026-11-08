// The site name in the header links home, like the logo next to it, so the click target is larger.
document$.subscribe(function () {
  var logo = document.querySelector(".md-header__button.md-logo");
  var name = document.querySelector(".md-header__title .md-header__topic:first-child .md-ellipsis");
  if (!logo || !name || name.querySelector("a")) return;
  var link = document.createElement("a");
  link.href = logo.getAttribute("href");
  link.className = "md-header__home";
  link.textContent = name.textContent.trim();
  link.setAttribute("aria-label", link.textContent + " home");
  name.replaceChildren(link);
});

// A back-to-top button in the bottom right corner once the reader has scrolled down.
(function () {
  var button = document.createElement("button");
  button.className = "to-top";
  button.type = "button";
  button.setAttribute("aria-label", "Back to top");
  button.innerHTML = '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M12 5.8 5.3 12.5l1.4 1.4L11 9.6V19h2V9.6l4.3 4.3 1.4-1.4z"/></svg>';
  button.addEventListener("click", function () { window.scrollTo({ top: 0, behavior: "smooth" }); });
  document.body.appendChild(button);
  function update() { button.classList.toggle("shown", window.scrollY > 500); }
  window.addEventListener("scroll", update, { passive: true });
  document$.subscribe(update);
})();

// The business e-mail address is assembled in the browser, so it is not in the page source for address harvesters.
document$.subscribe(function () {
  document.querySelectorAll(".contact-mail[data-user]").forEach(function (el) {
    var address = el.dataset.user + "@" + el.dataset.domain;
    var link = document.createElement("a");
    link.href = "mailto:" + address;
    link.textContent = address;
    el.replaceChildren(link);
    el.removeAttribute("data-user");
  });
});

// The home page header plays the Hermes artwork loop. Only after the page has loaded, never on small
// screens, with reduced motion or data saver (the poster stays), and paused while scrolled out of view.
document$.subscribe(function () {
  var video = document.querySelector(".hero-bg");
  if (!video) return;
  var calm = window.matchMedia("(prefers-reduced-motion: reduce)").matches ||
    window.matchMedia("(max-width: 760px)").matches ||
    (navigator.connection && navigator.connection.saveData);
  if (calm) return;
  function start() {
    video.querySelectorAll("source[data-src]").forEach(function (s) { s.src = s.dataset.src; s.removeAttribute("data-src"); });
    video.load();
    if (!("IntersectionObserver" in window)) { video.play().catch(function () {}); return; }
    new IntersectionObserver(function (entries) {
      entries.forEach(function (e) { if (e.isIntersecting) { video.play().catch(function () {}); } else { video.pause(); } });
    }).observe(video);
  }
  if (document.readyState === "complete") { setTimeout(start, 300); }
  else { window.addEventListener("load", function () { setTimeout(start, 300); }, { once: true }); }
});
