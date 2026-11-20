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

const rewardList = document.querySelector(".reward-card-grid");
const rewardViewToggle = document.querySelector(".reward-list-toolbar .list-view-toggle");

if (rewardList && rewardViewToggle) {
  // Shared with the React admin lists on the same origin.
  const storageKey = "pravburo.admin.listView";
  const buttons = rewardViewToggle.querySelectorAll("[data-list-mode]");
  let savedView;

  try {
    savedView = window.localStorage.getItem(storageKey);
  } catch {
    // Keep the default when browser storage is unavailable.
  }

  function showView(view) {
    rewardList.dataset.view = view;
    buttons.forEach((button) => {
      button.setAttribute("aria-pressed", String(button.dataset.listMode === view));
    });
  }

  showView(savedView === "rows" ? "rows" : "cards");
  buttons.forEach((button) => {
    button.addEventListener("click", () => {
      const view = button.dataset.listMode;
      showView(view);
      try {
        window.localStorage.setItem(storageKey, view);
      } catch {
        // The current view still works without browser storage.
      }
    });
  });
  rewardViewToggle.hidden = false;
}
