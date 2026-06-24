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

const deliveryAreas = Array.isArray(window.KARAVAGGIO_DELIVERY_AREAS)
  ? window.KARAVAGGIO_DELIVERY_AREAS
  : [];
const deliveryOptions = document.getElementById("delivery-area-options");

function normalizeSearchText(value) {
  return value
    .normalize("NFD")
    .replace(/[\u0300-\u036f]/g, "")
    .toUpperCase()
    .trim();
}

function getAreaLabel(area) {
  return `${area.city} - ${area.uf}`;
}

function findDeliveryArea(value) {
  const search = normalizeSearchText(value.replace(/\s+-\s+[A-Z]{2}$/i, ""));
  const selectedUf = value.match(/\s+-\s+([A-Z]{2})$/i)?.[1]?.toUpperCase();

  return deliveryAreas.find((area) => {
    const sameCity = normalizeSearchText(area.city) === search;
    const sameUf = !selectedUf || area.uf === selectedUf;
    return sameCity && sameUf;
  });
}

function renderAreaResult(target, area, typedValue) {
  if (!target) return;

  if (!typedValue.trim()) {
    target.replaceChildren();
    target.hidden = true;
    return;
  }

  if (!area) {
    target.innerHTML = "<strong>Cidade não encontrada</strong><span>Confira o nome ou selecione uma opção da lista.</span>";
    target.hidden = false;
    return;
  }

  target.innerHTML = `
    <strong>${getAreaLabel(area)}</strong>
    <span>Prazo: D+${area.prazo} dias úteis</span>
    <span>Praça: ${area.praca} | Filial: ${area.filial}</span>
    <span>Região: ${area.pracaComercial}</span>
    <span>CEP: ${area.cepInicial} a ${area.cepFinal}</span>
  `;
  target.hidden = false;
}

function bindAreaLookup(inputId, resultId) {
  const input = document.getElementById(inputId);
  const result = document.getElementById(resultId);
  if (!input || !result) return;

  const updateResult = () => renderAreaResult(result, findDeliveryArea(input.value), input.value);

  input.addEventListener("input", updateResult);
  input.addEventListener("change", updateResult);
}

if (deliveryOptions && deliveryAreas.length) {
  deliveryOptions.replaceChildren(
    ...deliveryAreas.map((area) => {
      const option = document.createElement("option");
      option.value = getAreaLabel(area);
      return option;
    })
  );
}

bindAreaLookup("delivery-city", "delivery-city-result");
bindAreaLookup("pickup-city", "pickup-city-result");
bindAreaLookup("quote-origin-city", "quote-origin-city-result");
bindAreaLookup("quote-destination-city", "quote-destination-city-result");

const carousel = document.querySelector(".services-carousel");
const track = carousel?.querySelector(".service-cards");
const cards = Array.from(carousel?.querySelectorAll(".service-card") || []);
const previousButton = carousel?.querySelector(".carousel-prev");
const nextButton = carousel?.querySelector(".carousel-next");
let activeService = 1;
let servicesAutoplay;

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

function showPreviousService() {
  activeService = (activeService - 1 + cards.length) % cards.length;
  updateServicesCarousel();
}

function showNextService() {
  activeService = (activeService + 1) % cards.length;
  updateServicesCarousel();
}

function startServicesAutoplay() {
  if (!carousel || cards.length <= 1 || servicesAutoplay) return;

  servicesAutoplay = window.setInterval(showNextService, 4500);
}

function stopServicesAutoplay() {
  window.clearInterval(servicesAutoplay);
  servicesAutoplay = undefined;
}

previousButton?.addEventListener("click", showPreviousService);
nextButton?.addEventListener("click", showNextService);

carousel?.addEventListener("mouseenter", stopServicesAutoplay);
carousel?.addEventListener("mouseleave", startServicesAutoplay);
carousel?.addEventListener("focusin", stopServicesAutoplay);
carousel?.addEventListener("focusout", startServicesAutoplay);
carousel?.addEventListener("touchstart", stopServicesAutoplay, { passive: true });
carousel?.addEventListener("touchend", startServicesAutoplay);

window.addEventListener("resize", updateServicesCarousel);
updateServicesCarousel();
startServicesAutoplay();
