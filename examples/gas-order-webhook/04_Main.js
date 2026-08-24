function doPost(event) {
  try {
    const requestBody = event && event.postData && event.postData.contents;
    if (!requestBody) {
      throw new Error('Request body is required');
    }
    const order = JSON.parse(requestBody);
    appendOrder_(order);
    notifyWebhook_(order);
    return jsonResponse_({ ok: true, orderId: order.id });
  } catch (error) {
    console.error(error);
    return jsonResponse_({ ok: false, error: String(error.message || error) });
  }
}

function jsonResponse_(body) {
  return ContentService.createTextOutput(JSON.stringify(body))
    .setMimeType(ContentService.MimeType.JSON);
}
