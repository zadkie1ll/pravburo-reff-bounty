var menuButton = document.querySelector(".menu-toggle");
var mainNavigation = document.querySelector(".main-nav");

if (menuButton && mainNavigation) {
  menuButton.addEventListener("click", function () {
    var isOpen = mainNavigation.classList.toggle("is-open");
    menuButton.setAttribute("aria-expanded", String(isOpen));
    menuButton.textContent = isOpen ? "×" : "☰";
  });
}

if (document.querySelector(".preview-banner")) {
  document.querySelectorAll("form").forEach(function (form) {
    form.addEventListener("submit", function (event) {
      event.preventDefault();
      window.alert("Это предпросмотр верстки. Данные не отправляются.");
    });
  });
}
