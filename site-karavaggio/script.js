const menuToggle = document.querySelector(".menu-toggle");
const mainMenu = document.querySelector(".main-menu");

menuToggle?.addEventListener("click", () => {
  const open = mainMenu.classList.toggle("is-open");
  menuToggle.setAttribute("aria-expanded", String(open));
});

mainMenu?.addEventListener("click", (event) => {
  if (event.target instanceof HTMLAnchorElement) {
    mainMenu.classList.remove("is-open");
    menuToggle?.setAttribute("aria-expanded", "false");
  }
});

document.querySelectorAll("form[data-success]").forEach((form) => {
  form.addEventListener("submit", (event) => {
    event.preventDefault();
    const status = form.querySelector(".form-status");
    if (status) {
      status.textContent = form.dataset.success || "Enviado com sucesso.";
    }
    const successCard = form.querySelector(".success-card");
    if (successCard) {
      successCard.hidden = false;
    }
    form.reset();
  });
});

document.querySelectorAll("form[data-success-target]").forEach((form) => {
  form.addEventListener("submit", (event) => {
    event.preventDefault();
    const template = document.getElementById(form.dataset.successTarget);
    if (!(template instanceof HTMLTemplateElement)) return;

    const success = template.content.firstElementChild.cloneNode(true);
    const section = form.closest(".section");

    if (section) {
      section.replaceChildren(success);
      section.classList.add("success-only");
    } else {
      form.replaceWith(success);
    }

    form.reset();
    success.scrollIntoView({ behavior: "smooth", block: "center" });
    success.focus({ preventScroll: true });
  });
});

const carousel = document.querySelector(".services-carousel");
const track = carousel?.querySelector(".service-cards");
const cards = Array.from(carousel?.querySelectorAll(".service-card") || []);
const previousButton = carousel?.querySelector(".carousel-prev");
const nextButton = carousel?.querySelector(".carousel-next");
let activeService = 1;

function updateServicesCarousel() {
  if (!track || cards.length === 0) return;

  const carouselWidth = carousel.getBoundingClientRect().width;
  const carouselStyles = getComputedStyle(carousel);
  const carouselPaddingLeft = parseFloat(carouselStyles.paddingLeft) || 0;
  const cardWidth = cards[0].getBoundingClientRect().width;
  const gap = parseFloat(getComputedStyle(track).gap) || 0;
  const activeCenter = activeService * (cardWidth + gap) + cardWidth / 2;
  const carouselCenter = carouselWidth / 2;

  track.style.transform = `translateX(${carouselCenter - carouselPaddingLeft - activeCenter}px)`;
  cards.forEach((card, index) => {
    card.classList.toggle("is-active", index === activeService);
  });
}

previousButton?.addEventListener("click", () => {
  activeService = (activeService - 1 + cards.length) % cards.length;
  updateServicesCarousel();
});

nextButton?.addEventListener("click", () => {
  activeService = (activeService + 1) % cards.length;
  updateServicesCarousel();
});

window.addEventListener("resize", updateServicesCarousel);
updateServicesCarousel();
