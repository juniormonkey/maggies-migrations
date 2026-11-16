/**
 * @OnlyCurrentDoc
 */

const SHEET_NAME = 'Pins';
const SECRET_PROP = 'WEBHOOK_SECRET';
const LAST_MSG_PROP = 'LAST_DISCORD_MESSAGE_ID';

const HEADERS = [
  'id',
  'status',
  'place_name',
  'location_query',
  'lat',
  'lng',
  'caption',
  'author',
  'posted_at',
  'layer',
  'media_urls',
  'discord_url',
  'notes',
];

function onOpen() {
  SpreadsheetApp.getUi()
    .createMenu('Maggie Map')
    .addItem('Set Webhook Secret', 'promptSetWebhookSecret')
    .addToUi();
}

function promptSetWebhookSecret() {
  const ui = SpreadsheetApp.getUi();
  const props = PropertiesService.getScriptProperties();
  const current = props.getProperty(SECRET_PROP);
  const promptMsg = current
    ? 'A webhook secret is currently set. Enter a new GSHEET_WEBHOOK_SECRET to replace it:'
    : 'Paste the GSHEET_WEBHOOK_SECRET value provided during setup:';
  const result = ui.prompt('Set Webhook Secret', promptMsg, ui.ButtonSet.OK_CANCEL);
  if (result.getSelectedButton() === ui.Button.OK) {
    const val = result.getResponseText().trim();
    if (!val) {
      ui.alert('Secret cannot be empty.');
      return;
    }
    props.setProperty(SECRET_PROP, val);
    ui.alert('Webhook secret saved.');
  }
}

function doGet(e) {
  try {
    const pins = getApprovedPins_();
    return jsonResponse_({ status: 'ok', pins });
  } catch (err) {
    return jsonResponse_({ status: 'error', error: String(err) });
  }
}

function doPost(e) {
  try {
    const payload = JSON.parse((e && e.postData && e.postData.contents) || '{}');
    const configuredSecret = PropertiesService.getScriptProperties().getProperty(SECRET_PROP);
    if (!configuredSecret || payload.secret !== configuredSecret) {
      return jsonResponse_({ status: 'error', error: 'unauthorized' });
    }

    const action = payload.action || '';
    if (action === 'get_state') {
      return jsonResponse_(handleGetState_());
    }
    if (action === 'sync_batch') {
      return jsonResponse_(handleSyncBatch_(payload));
    }
    return jsonResponse_({ status: 'error', error: `unknown action: ${action}` });
  } catch (err) {
    return jsonResponse_({ status: 'error', error: String(err) });
  }
}

function getPinsSheet_() {
  const ss = SpreadsheetApp.getActiveSpreadsheet();
  let sheet = ss.getSheetByName(SHEET_NAME);
  if (!sheet) {
    sheet = ss.getSheets()[0];
    sheet.setName(SHEET_NAME);
  }
  if (sheet.getLastRow() === 0) {
    sheet.appendRow(HEADERS);
  }
  return sheet;
}

function splitMediaUrls_(raw) {
  return String(raw || '')
    .split(/\s+/)
    .map((s) => s.trim())
    .filter(Boolean);
}

function getSheetRows_() {
  const sheet = getPinsSheet_();
  const lastRow = sheet.getLastRow();
  if (lastRow < 2) {
    return { sheet, rows: [] };
  }
  const values = sheet.getRange(2, 1, lastRow - 1, HEADERS.length).getValues();
  const rows = values.map((vals, idx) => ({
    rowNumber: idx + 2,
    id: String(vals[0] || '').trim(),
    status: String(vals[1] || '').trim().toLowerCase(),
    place_name: String(vals[2] || '').trim(),
    location_query: String(vals[3] || '').trim(),
    lat: vals[4],
    lng: vals[5],
    caption: String(vals[6] || ''),
    author: String(vals[7] || '').trim(),
    posted_at:
      vals[8] instanceof Date
        ? Utilities.formatDate(vals[8], 'UTC', 'yyyy-MM-dd')
        : String(vals[8] || '').trim(),
    layer: String(vals[9] || '').trim(),
    media_urls: String(vals[10] || '').trim(),
    discord_url: String(vals[11] || '').trim(),
    notes: String(vals[12] || '').trim(),
  }));
  return { sheet, rows };
}

function getApprovedPins_() {
  const { rows } = getSheetRows_();
  return rows
    .filter((r) => {
      const latNum = Number(r.lat);
      const lngNum = Number(r.lng);
      return (
        r.status === 'approved' &&
        r.lat !== '' &&
        r.lng !== '' &&
        !Number.isNaN(latNum) &&
        !Number.isNaN(lngNum)
      );
    })
    .map((r) => ({
      id: r.id,
      place_name: r.place_name,
      lat: Number(r.lat),
      lng: Number(r.lng),
      caption: r.caption,
      author: r.author,
      posted_at: r.posted_at,
      layer: r.layer,
      media: splitMediaUrls_(r.media_urls),
    }));
}

function handleGetState_() {
  const props = PropertiesService.getScriptProperties();
  const lastMessageId = props.getProperty(LAST_MSG_PROP) || '';
  const { rows } = getSheetRows_();

  const existingIds = rows.map((r) => r.id).filter(Boolean);
  const retryGeocoding = rows
    .filter(
      (r) =>
        (r.status === 'needs_geocoding' || r.status === 'pending') &&
        r.location_query &&
        (r.lat === '' || r.lng === '' || Number.isNaN(Number(r.lat)) || Number.isNaN(Number(r.lng)))
    )
    .map((r) => ({
      id: r.id,
      location_query: r.location_query,
      place_name: r.place_name,
      author: r.author,
      posted_at: r.posted_at,
      caption: r.caption,
      media_urls: r.media_urls,
      discord_url: r.discord_url,
    }));

  return {
    status: 'ok',
    last_message_id: lastMessageId,
    existing_ids: existingIds,
    retry_geocoding: retryGeocoding,
    approved_pins: getApprovedPins_(),
  };
}

function handleSyncBatch_(payload) {
  const { sheet, rows } = getSheetRows_();
  const rowById = {};
  rows.forEach((r) => {
    if (r.id) rowById[r.id] = r;
  });

  const geocodedUpdates = Array.isArray(payload.geocoded_updates) ? payload.geocoded_updates : [];
  let updatedCount = 0;
  geocodedUpdates.forEach((upd) => {
    const existing = rowById[String(upd.id || '')];
    if (!existing) return;
    if (upd.place_name) {
      sheet.getRange(existing.rowNumber, 3).setValue(upd.place_name);
    }
    sheet.getRange(existing.rowNumber, 2).setValue(upd.status || 'pending');
    sheet.getRange(existing.rowNumber, 5).setValue(upd.lat);
    sheet.getRange(existing.rowNumber, 6).setValue(upd.lng);
    sheet
      .getRange(existing.rowNumber, 13)
      .setValue(upd.notes || 'Geocoded from updated location_query — change status to approved');
    updatedCount += 1;
  });

  const newRows = Array.isArray(payload.new_rows) ? payload.new_rows : [];
  let appendedCount = 0;
  newRows.forEach((row) => {
    const id = String(row.id || '').trim();
    if (!id || rowById[id]) return;
    const values = HEADERS.map((h) => (row[h] !== undefined && row[h] !== null ? row[h] : ''));
    sheet.appendRow(values);
    rowById[id] = true;
    appendedCount += 1;
  });

  if (payload.last_message_id) {
    PropertiesService.getScriptProperties().setProperty(
      LAST_MSG_PROP,
      String(payload.last_message_id)
    );
  }

  return {
    status: 'ok',
    sheet_url: SpreadsheetApp.getActiveSpreadsheet().getUrl(),
    appended_count: appendedCount,
    updated_count: updatedCount,
    approved_pins: getApprovedPins_(),
  };
}

function jsonResponse_(obj) {
  return ContentService.createTextOutput(JSON.stringify(obj)).setMimeType(
    ContentService.MimeType.JSON
  );
}
