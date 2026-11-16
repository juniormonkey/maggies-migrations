const appShell = document.getElementById('app');
const drawerToggle = document.getElementById('drawer-toggle');
const pinListEl = document.getElementById('pin-list');
const pinCountEl = document.getElementById('pin-count');
const searchInput = document.getElementById('search-input');
const syncStatusEl = document.getElementById('sync-status');
const titleEl = document.getElementById('map-title');
const subtitleEl = document.getElementById('map-subtitle');

const lightbox = document.getElementById('lightbox');
const lightboxTitle = document.getElementById('lightbox-title');
const lightboxMeta = document.getElementById('lightbox-meta');
const lightboxSlides = document.getElementById('lightbox-slides');
const lightboxCaption = document.getElementById('lightbox-caption');
const slideCounter = document.getElementById('slide-counter');
const externalMediaLink = document.getElementById('external-media-link');
const prevSlideBtn = document.getElementById('prev-slide');
const nextSlideBtn = document.getElementById('next-slide');

let allPins = [];
let activePinId = null;
let currentSlides = [];
let currentSlideIndex = 0;

if (window.matchMedia('(max-width: 680px)').matches) {
  appShell.classList.add('drawer-collapsed');
  drawerToggle.setAttribute('aria-expanded', 'false');
}

drawerToggle.addEventListener('click', () => {
  const collapsed = appShell.classList.toggle('drawer-collapsed');
  drawerToggle.setAttribute('aria-expanded', String(!collapsed));
});

const map = L.map('map', { zoomControl: true }).setView([25, -20], 2);

L.tileLayer('https://tile.openstreetmap.org/{z}/{x}/{y}.png', {
  attribution: '&copy; OpenStreetMap contributors',
  maxZoom: 19,
}).addTo(map);

const markerLayer = L.layerGroup().addTo(map);
const markerById = new Map();

function createPinIcon() {
  const svg =
    '<svg xmlns="http://www.w3.org/2000/svg" width="30" height="40" viewBox="0 0 30 40">' +
    '<path d="M15 0C6.7 0 0 6.7 0 15c0 10.5 15 25 15 25s15-14.5 15-25C30 6.7 23.3 0 15 0z" fill="#0288d1" stroke="#0f172a" stroke-width="2"/>' +
    '<circle cx="15" cy="14" r="5.5" fill="#ffffff"/>' +
    '<circle cx="15" cy="14" r="2.5" fill="#0f172a"/>' +
    '</svg>';
  return L.icon({
    iconUrl: 'data:image/svg+xml;charset=UTF-8,' + encodeURIComponent(svg),
    iconSize: [30, 40],
    iconAnchor: [15, 40],
    popupAnchor: [0, -36],
  });
}

const magpieIcon = createPinIcon();

function extractYouTubeId(url) {
  const match = String(url).match(
    /(?:youtube\.com\/(?:embed\/|shorts\/|watch\?v=)|youtu\.be\/)([A-Za-z0-9_-]{6,})/
  );
  return match ? match[1] : null;
}

function isSafeMediaUrl(url) {
  return typeof url === 'string' && (url.startsWith('https://') || url.startsWith('./photos/'));
}

function getThumbnailUrl(mediaUrl) {
  const ytId = extractYouTubeId(mediaUrl);
  if (ytId) {
    return { url: `https://img.youtube.com/vi/${ytId}/hqdefault.jpg`, isVideo: true };
  }
  return { url: mediaUrl, isVideo: false };
}

function spreadCoincidentCoords(pins) {
  const buckets = new Map();
  return pins.map((pin) => {
    const key = `${Number(pin.lat).toFixed(4)},${Number(pin.lng).toFixed(4)}`;
    const idx = buckets.get(key) || 0;
    buckets.set(key, idx + 1);
    if (idx === 0) {
      return { ...pin, displayLat: Number(pin.lat), displayLng: Number(pin.lng) };
    }
    const angle = idx * 2.39996;
    const radius = 0.0003 * Math.sqrt(idx);
    return {
      ...pin,
      displayLat: Number(pin.lat) + radius * Math.cos(angle),
      displayLng: Number(pin.lng) + radius * Math.sin(angle),
    };
  });
}

function formatMetaLine(pin) {
  const parts = [];
  if (pin.author) parts.push(pin.author);
  if (pin.posted_at) parts.push(pin.posted_at);
  if (pin.layer) parts.push(pin.layer);
  return parts.join(' · ');
}

function buildPopupElement(pin) {
  const card = document.createElement('div');
  card.className = 'popup-card';

  const heading = document.createElement('h3');
  heading.textContent = pin.place_name || 'Maggie sighting';
  card.appendChild(heading);

  const metaText = formatMetaLine(pin);
  if (metaText) {
    const meta = document.createElement('p');
    meta.className = 'popup-meta';
    meta.textContent = metaText;
    card.appendChild(meta);
  }

  if (pin.caption) {
    const cap = document.createElement('p');
    cap.className = 'popup-caption';
    cap.textContent = pin.caption;
    card.appendChild(cap);
  }

  const media = (pin.media || []).filter(isSafeMediaUrl);
  if (media.length > 0) {
    const grid = document.createElement('div');
    grid.className = 'popup-thumbs';
    media.slice(0, 6).forEach((mediaUrl, idx) => {
      const btn = document.createElement('button');
      btn.type = 'button';
      btn.className = 'popup-thumb-btn';
      btn.setAttribute('aria-label', `Open media ${idx + 1} for ${pin.place_name}`);
      const thumb = getThumbnailUrl(mediaUrl);
      const img = document.createElement('img');
      img.src = thumb.url;
      img.alt = `${pin.place_name} photo ${idx + 1}`;
      btn.appendChild(img);
      if (thumb.isVideo) {
        const badge = document.createElement('span');
        badge.className = 'video-badge';
        badge.textContent = 'Video';
        btn.appendChild(badge);
      }
      btn.addEventListener('click', () => openLightbox(pin, idx));
      grid.appendChild(btn);
    });
    card.appendChild(grid);

    const openBtn = document.createElement('button');
    openBtn.type = 'button';
    openBtn.className = 'popup-open-btn';
    openBtn.textContent = media.length === 1 ? 'View media' : `View all ${media.length} items`;
    openBtn.addEventListener('click', () => openLightbox(pin, 0));
    card.appendChild(openBtn);
  }

  return card;
}

function renderList(pins) {
  pinListEl.replaceChildren();
  pinCountEl.textContent = `${pins.length} ${pins.length === 1 ? 'pin' : 'pins'}`;

  for (const pin of pins) {
    const li = document.createElement('li');
    const btn = document.createElement('button');
    btn.type = 'button';
    btn.className = 'pin-item';
    btn.dataset.pinId = pin.id;
    if (pin.id === activePinId) {
      btn.setAttribute('aria-current', 'true');
    }

    const media = (pin.media || []).filter(isSafeMediaUrl);
    if (media.length > 0) {
      const thumbWrap = document.createElement('div');
      thumbWrap.className = 'pin-thumb-wrap';
      const thumbInfo = getThumbnailUrl(media[0]);
      const img = document.createElement('img');
      img.className = 'pin-thumb';
      img.src = thumbInfo.url;
      img.alt = '';
      img.width = 56;
      img.height = 56;
      img.loading = 'lazy';
      thumbWrap.appendChild(img);
      if (thumbInfo.isVideo) {
        const badge = document.createElement('span');
        badge.className = 'video-badge';
        badge.textContent = 'YT';
        thumbWrap.appendChild(badge);
      }
      btn.appendChild(thumbWrap);
    }

    const info = document.createElement('div');
    info.className = 'pin-info';
    const title = document.createElement('div');
    title.className = 'pin-title';
    title.textContent = pin.place_name || 'Unknown location';
    const meta = document.createElement('div');
    meta.className = 'pin-meta';
    const mediaLabel = media.length > 0 ? `${media.length} media` : 'No photo';
    meta.textContent = [pin.posted_at || pin.layer, mediaLabel].filter(Boolean).join(' · ');

    info.appendChild(title);
    info.appendChild(meta);
    btn.appendChild(info);

    btn.addEventListener('click', () => selectPin(pin.id));
    li.appendChild(btn);
    pinListEl.appendChild(li);
  }
}

function renderMap(pins, shouldFitBounds = true) {
  markerLayer.clearLayers();
  markerById.clear();

  const positioned = spreadCoincidentCoords(pins);
  const bounds = [];

  for (const pin of positioned) {
    if (!Number.isFinite(pin.displayLat) || !Number.isFinite(pin.displayLng)) {
      continue;
    }
    const marker = L.marker([pin.displayLat, pin.displayLng], { icon: magpieIcon });
    marker.bindPopup(() => buildPopupElement(pin), { maxWidth: 270 });
    marker.on('click', () => {
      activePinId = pin.id;
      highlightListItem(pin.id);
    });
    marker.addTo(markerLayer);
    markerById.set(pin.id, marker);
    bounds.push([pin.displayLat, pin.displayLng]);
  }

  if (shouldFitBounds && bounds.length > 0) {
    map.fitBounds(bounds, { padding: [40, 40], maxZoom: 12 });
  }
}

function highlightListItem(pinId) {
  for (const el of pinListEl.querySelectorAll('.pin-item')) {
    if (el.dataset.pinId === pinId) {
      el.setAttribute('aria-current', 'true');
    } else {
      el.removeAttribute('aria-current');
    }
  }
}

function selectPin(pinId) {
  activePinId = pinId;
  highlightListItem(pinId);
  const marker = markerById.get(pinId);
  if (marker) {
    map.setView(marker.getLatLng(), Math.max(map.getZoom(), 9), { animate: true });
    marker.openPopup();
  }
  if (window.matchMedia('(max-width: 680px)').matches) {
    appShell.classList.add('drawer-collapsed');
    drawerToggle.setAttribute('aria-expanded', 'false');
  }
}

function renderActiveSlide() {
  const slideNodes = lightboxSlides.querySelectorAll('.lightbox-slide');
  slideNodes.forEach((node, idx) => {
    const isActive = idx === currentSlideIndex;
    node.hidden = !isActive;
    const ytId = node.dataset.youtubeId;
    if (ytId) {
      if (isActive && !node.querySelector('iframe')) {
        const iframe = document.createElement('iframe');
        iframe.src = `https://www.youtube-nocookie.com/embed/${ytId}`;
        iframe.title = 'Maggie video';
        iframe.allow = 'accelerometer; autoplay; clipboard-write; encrypted-media; gyroscope; picture-in-picture';
        iframe.allowFullscreen = true;
        node.replaceChildren(iframe);
      } else if (!isActive) {
        node.replaceChildren();
      }
    }
  });

  const currentUrl = currentSlides[currentSlideIndex];
  slideCounter.textContent = `${currentSlideIndex + 1} / ${currentSlides.length}`;
  prevSlideBtn.hidden = currentSlides.length <= 1;
  nextSlideBtn.hidden = currentSlides.length <= 1;
  if (currentUrl) {
    externalMediaLink.href = currentUrl;
    externalMediaLink.hidden = false;
  } else {
    externalMediaLink.hidden = true;
  }
}

function openLightbox(pin, startIndex = 0) {
  currentSlides = (pin.media || []).filter(isSafeMediaUrl);
  if (currentSlides.length === 0) return;

  currentSlideIndex = Math.max(0, Math.min(startIndex, currentSlides.length - 1));
  lightboxTitle.textContent = pin.place_name || 'Maggie sighting';
  lightboxMeta.textContent = formatMetaLine(pin);
  lightboxCaption.textContent = pin.caption || '';

  lightboxSlides.replaceChildren();
  currentSlides.forEach((mediaUrl, idx) => {
    const slide = document.createElement('div');
    slide.className = 'lightbox-slide';
    slide.hidden = idx !== currentSlideIndex;

    const ytId = extractYouTubeId(mediaUrl);
    if (ytId) {
      slide.dataset.youtubeId = ytId;
    } else {
      const img = document.createElement('img');
      img.src = mediaUrl;
      img.alt = `${pin.place_name} photo ${idx + 1}`;
      if (idx !== currentSlideIndex) {
        img.setAttribute('fetchpriority', 'low');
      }
      slide.appendChild(img);
    }
    lightboxSlides.appendChild(slide);
  });

  renderActiveSlide();
  lightbox.showModal();
}

prevSlideBtn.addEventListener('click', () => {
  if (currentSlides.length <= 1) return;
  currentSlideIndex = (currentSlideIndex - 1 + currentSlides.length) % currentSlides.length;
  renderActiveSlide();
});

nextSlideBtn.addEventListener('click', () => {
  if (currentSlides.length <= 1) return;
  currentSlideIndex = (currentSlideIndex + 1) % currentSlides.length;
  renderActiveSlide();
});

lightbox.addEventListener('keydown', (event) => {
  if (event.key === 'ArrowLeft') {
    prevSlideBtn.click();
  } else if (event.key === 'ArrowRight') {
    nextSlideBtn.click();
  }
});

lightbox.addEventListener('close', () => {
  lightbox.returnValue = '';
  lightboxSlides.replaceChildren();
});

if (!('closedBy' in HTMLDialogElement.prototype)) {
  const outside = (e, dialog) => {
    const r = dialog.getBoundingClientRect();
    return e.clientX < r.left || e.clientX > r.right || e.clientY < r.top || e.clientY > r.bottom;
  };
  let pressedOutside = false;
  lightbox.addEventListener('pointerdown', (e) => {
    pressedOutside = outside(e, lightbox);
  });
  lightbox.addEventListener('pointerup', (e) => {
    if (pressedOutside && outside(e, lightbox)) {
      lightbox.close();
    }
  });
}

function applyFilter(fit = false) {
  const q = searchInput.value.trim().toLowerCase();
  const filtered = q
    ? allPins.filter((pin) =>
        [pin.place_name, pin.caption, pin.author, pin.layer]
          .filter(Boolean)
          .some((val) => String(val).toLowerCase().includes(q))
      )
    : allPins;
  renderList(filtered);
  renderMap(filtered, fit);
}

searchInput.addEventListener('input', () => applyFilter(false));

async function init() {
  try {
    const [pinsResp, cfgResp] = await Promise.all([
      fetch('./data/pins.json', { cache: 'no-cache' }),
      fetch('./data/config.json', { cache: 'no-cache' }).catch(() => null),
    ]);

    if (cfgResp && cfgResp.ok) {
      const cfg = await cfgResp.json();
      if (cfg.mapTitle) titleEl.textContent = cfg.mapTitle;
      if (cfg.mapSubtitle) subtitleEl.textContent = cfg.mapSubtitle;
      if (pinsResp.ok) {
        allPins = await pinsResp.json();
        applyFilter(true);
      }

      if (cfg.liveSheetUrl) {
        syncStatusEl.textContent = 'Syncing live...';
        const separator = cfg.liveSheetUrl.includes('?') ? '&' : '?';
        const liveResp = await fetch(`${cfg.liveSheetUrl}${separator}action=pins`, {
          cache: 'no-store',
        });
        if (liveResp.ok) {
          const payload = await liveResp.json();
          if (payload.status === 'ok' && Array.isArray(payload.pins) && payload.pins.length > 0) {
            allPins = payload.pins;
            applyFilter(false);
            syncStatusEl.textContent = 'Live sheet synced';
          } else {
            syncStatusEl.textContent = '';
          }
        }
      }
    } else if (pinsResp.ok) {
      allPins = await pinsResp.json();
      applyFilter(true);
    }
  } catch (err) {
    console.error('Failed to initialize map data:', err);
    syncStatusEl.textContent = 'Snapshot mode';
  }
}

init();
