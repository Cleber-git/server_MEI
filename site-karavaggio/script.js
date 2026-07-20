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

function onlyDigits(value, maxLength) {
  const digits = value.replace(/\D/g, "");
  return typeof maxLength === "number" ? digits.slice(0, maxLength) : digits;
}

function formatCpfCnpj(value) {
  const digits = onlyDigits(value, 14);

  if (digits.length <= 11) {
    return digits
      .replace(/(\d{3})(\d)/, "$1.$2")
      .replace(/(\d{3})(\d)/, "$1.$2")
      .replace(/(\d{3})(\d{1,2})$/, "$1-$2");
  }

  return digits
    .replace(/^(\d{2})(\d)/, "$1.$2")
    .replace(/^(\d{2})\.(\d{3})(\d)/, "$1.$2.$3")
    .replace(/\.(\d{3})(\d)/, ".$1/$2")
    .replace(/(\d{4})(\d{1,2})$/, "$1-$2");
}

function formatMoney(value) {
  const digits = onlyDigits(value);
  if (!digits) return "";

  const amount = Number(digits) / 100;
  return amount.toLocaleString("pt-BR", {
    style: "currency",
    currency: "BRL",
    minimumFractionDigits: 2,
  });
}

function formatWeight(value) {
  const digits = onlyDigits(value);
  if (!digits) return "";

  const integerPart = digits.slice(0, -2) || "0";
  const decimalPart = digits.slice(-2).padStart(2, "0");
  return `${Number(integerPart).toLocaleString("pt-BR")},${decimalPart} kg`;
}

function formatPhone(value) {
  const digits = onlyDigits(value, 11);

  if (digits.length <= 10) {
    return digits
      .replace(/^(\d{2})(\d)/, "($1) $2")
      .replace(/(\d{4})(\d{1,4})$/, "$1-$2");
  }

  return digits
    .replace(/^(\d{2})(\d)/, "($1) $2")
    .replace(/(\d{5})(\d{1,4})$/, "$1-$2");
}

function bindInstantMask(selector, formatter) {
  document.querySelectorAll(selector).forEach((input) => {
    input.addEventListener("input", () => {
      input.value = formatter(input.value);
    });
  });
}

bindInstantMask("[data-document-mask]", formatCpfCnpj);
bindInstantMask("[data-money-mask]", formatMoney);
bindInstantMask("[data-weight-mask]", formatWeight);
bindInstantMask("[data-phone-mask]", formatPhone);
bindInstantMask("[data-integer-mask]", (value) => onlyDigits(value));
bindInstantMask('input[type="email"]', (value) => value.replace(/\s/g, "").toLowerCase());

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
  form.addEventListener("submit", async (event) => {
    event.preventDefault();

    try {
      await sendQuoteEmail(form);
    } catch (error) {
      alert(error.message || "Não foi possível enviar a solicitação. Tente novamente.");
      return;
    }

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

function getFieldValue(form, name) {
  return form.elements[name]?.value?.trim() || "";
}

function getQuoteApiUrl() {
  const apiUrl = window.KARAVAGGIO_CONFIG?.quoteApiUrl?.trim();
  if (!apiUrl) {
    throw new Error("Serviço de cotação não configurado.");
  }
  return apiUrl;
}

async function sendQuoteEmail(form) {
  const payload = {
    cnpj_pagador: getFieldValue(form, "cnpj_pagador"),
    cnpj_origem: getFieldValue(form, "cnpj_origem"),
    origem: getFieldValue(form, "origem"),
    cnpj_destino: getFieldValue(form, "cnpj_destino"),
    destino: getFieldValue(form, "destino"),
    valor_nota: getFieldValue(form, "valor_nota"),
    volumes: getFieldValue(form, "volumes"),
    peso_bruto: getFieldValue(form, "peso_bruto"),
    cubagem: getFieldValue(form, "cubagem"),
    observacoes: getFieldValue(form, "observacoes"),
  };

  const response = await fetch(getQuoteApiUrl(), {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
    },
    body: JSON.stringify(payload),
  });

  if (!response.ok) {
    const error = await response.json().catch(() => ({}));
    throw new Error(error.detail || "Falha ao enviar cotação.");
  }
}

const deliveryAreas = Array.isArray(window.KARAVAGGIO_DELIVERY_AREAS)
  ? window.KARAVAGGIO_DELIVERY_AREAS
  : [];
const pickupAreas = Array.isArray(window.KARAVAGGIO_PICKUP_AREAS)
  ? window.KARAVAGGIO_PICKUP_AREAS
  : [];
const deliveryOptions = document.getElementById("delivery-area-options");
const pickupOptions = document.getElementById("pickup-area-options");

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

function findArea(value, areas) {
  const search = normalizeSearchText(value.replace(/\s+-\s+[A-Z]{2}$/i, ""));
  const selectedUf = value.match(/\s+-\s+([A-Z]{2})$/i)?.[1]?.toUpperCase();

  return areas.find((area) => {
    const sameCity = normalizeSearchText(area.city) === search;
    const sameUf = !selectedUf || area.uf === selectedUf;
    return sameCity && sameUf;
  });
}

function renderAreaResult(target, area, typedValue, options = {}) {
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

  if (options.showDetails === false) {
    target.replaceChildren();
    target.hidden = true;
    return;
  }

  const details = [];

  if (options.showPrazo !== false) {
    details.push(`<span>Prazo: D+${area.prazo} dias úteis</span>`);
  }

  if (options.showPraca !== false) {
    details.push(`<span>Praça: ${area.praca} | Filial: ${area.filial}</span>`);
  } else {
    details.push(`<span>Filial: ${area.filial}</span>`);
  }

  details.push(`<span>Região: ${area.pracaComercial}</span>`);
  details.push(`<span>CEP: ${area.cepInicial} a ${area.cepFinal}</span>`);

  target.innerHTML = `
    <strong>${getAreaLabel(area)}</strong>
    ${details.join("")}
  `;
  target.hidden = false;
}

function bindAreaLookup(inputId, resultId, options = {}, areas = deliveryAreas) {
  const input = document.getElementById(inputId);
  const result = document.getElementById(resultId);
  if (!input || !result) return;

  const updateResult = () => renderAreaResult(result, findArea(input.value, areas), input.value, options);

  input.addEventListener("input", updateResult);
  input.addEventListener("change", updateResult);
}

function populateAreaOptions(target, areas) {
  if (!target || !areas.length) return;

  target.replaceChildren(
    ...areas.map((area) => {
      const option = document.createElement("option");
      option.value = getAreaLabel(area);
      return option;
    })
  );
}

populateAreaOptions(deliveryOptions, deliveryAreas);
populateAreaOptions(pickupOptions, pickupAreas);

bindAreaLookup("delivery-city", "delivery-city-result", { showPraca: false });
bindAreaLookup("pickup-city", "pickup-city-result", { showPraca: false, showPrazo: false }, pickupAreas);
bindAreaLookup("quote-origin-city", "quote-origin-city-result", { showDetails: false }, pickupAreas);
bindAreaLookup("quote-destination-city", "quote-destination-city-result", { showDetails: false }, pickupAreas);

const carousel = document.querySelector(".services-carousel");
const track = carousel?.querySelector(".service-cards");
const cards = Array.from(carousel?.querySelectorAll(".service-card") || []);
const previousButton = carousel?.querySelector(".carousel-prev");
const nextButton = carousel?.querySelector(".carousel-next");
let activeService = 0;
let isServiceCarouselMoving = false;
const serviceCarouselDuration = 420;

function getServiceCardScale(card) {
  return card.classList.contains("is-active") ? 1 : 0.76;
}

function getServiceCardOpacity(card) {
  return card.classList.contains("is-active") ? 1 : 0.52;
}

function getVisibleServiceRects() {
  return new Map(
    cards
      .filter((card) => !card.classList.contains("is-hidden"))
      .map((card) => [card, {
        left: card.getBoundingClientRect().left,
        scale: getServiceCardScale(card),
        opacity: getServiceCardOpacity(card),
      }])
  );
}

function updateServicesCarousel({ animate = false, direction = 1, previousRects = null } = {}) {
  if (!track || cards.length === 0) return;

  const previousService = (activeService - 1 + cards.length) % cards.length;
  const nextService = (activeService + 1) % cards.length;

  cards.forEach((card, index) => {
    const isPrevious = index === previousService;
    const isActive = index === activeService;
    const isNext = index === nextService;

    card.classList.toggle("is-prev", isPrevious);
    card.classList.toggle("is-active", isActive);
    card.classList.toggle("is-next", isNext);
    card.classList.toggle("is-hidden", !isPrevious && !isActive && !isNext);

    if (isPrevious) card.style.order = "0";
    if (isActive) card.style.order = "1";
    if (isNext) card.style.order = "2";
  });

  if (!animate || !previousRects) return;

  const gap = parseFloat(getComputedStyle(track).gap) || 0;
  const cardWidth = cards.find((card) => !card.classList.contains("is-hidden"))?.getBoundingClientRect().width || 0;
  const entryOffset = direction * (cardWidth + gap);

  cards
    .filter((card) => !card.classList.contains("is-hidden"))
    .forEach((card) => {
      const targetRect = card.getBoundingClientRect();
      const targetScale = getServiceCardScale(card);
      const targetOpacity = getServiceCardOpacity(card);
      const previousRect = previousRects.get(card);
      const startX = previousRect ? previousRect.left - targetRect.left : entryOffset;
      const startScale = previousRect ? previousRect.scale : targetScale;
      const startOpacity = previousRect ? previousRect.opacity : 0;

      card.animate(
        [
          {
            opacity: startOpacity,
            transform: `translateX(${startX}px) scale(${startScale})`,
          },
          {
            opacity: targetOpacity,
            transform: `translateX(0) scale(${targetScale})`,
          },
        ],
        {
          duration: serviceCarouselDuration,
          easing: "cubic-bezier(0.22, 1, 0.36, 1)",
        }
      );
    });
}

function moveServicesCarousel(direction) {
  if (isServiceCarouselMoving || cards.length === 0) return;

  isServiceCarouselMoving = true;
  const previousRects = getVisibleServiceRects();
  activeService = (activeService + direction + cards.length) % cards.length;
  updateServicesCarousel({ animate: true, direction, previousRects });

  window.setTimeout(() => {
    isServiceCarouselMoving = false;
  }, serviceCarouselDuration);
}

function showPreviousService() {
  moveServicesCarousel(-1);
}

function showNextService() {
  moveServicesCarousel(1);
}

previousButton?.addEventListener("click", showPreviousService);
nextButton?.addEventListener("click", showNextService);

window.addEventListener("resize", updateServicesCarousel);
updateServicesCarousel();
