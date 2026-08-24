function getOrdersSheet_() {
  const spreadsheet = SpreadsheetApp.getActiveSpreadsheet();
  const sheet = spreadsheet.getSheetByName(CONFIG.SHEET_NAME);
  if (!sheet) {
    throw new Error(`Required sheet not found: ${CONFIG.SHEET_NAME}`);
  }
  return sheet;
}

function appendOrder_(order) {
  if (!order || typeof order.id !== 'string' || !order.id.trim()) {
    throw new Error('order.id must be a non-empty string');
  }
  const status = typeof order.status === 'string' ? order.status : 'RECEIVED';
  const payload = JSON.stringify(order);
  getOrdersSheet_().appendRow([order.id.trim(), status, payload, new Date().toISOString()]);
}
