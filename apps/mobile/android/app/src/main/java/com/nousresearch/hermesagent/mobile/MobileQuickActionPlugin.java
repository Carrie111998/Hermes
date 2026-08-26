package com.nousresearch.hermesagent.mobile;

import android.content.Intent;

import com.getcapacitor.JSObject;
import com.getcapacitor.Plugin;
import com.getcapacitor.PluginCall;
import com.getcapacitor.PluginMethod;
import com.getcapacitor.annotation.CapacitorPlugin;

/** Exposes only explicit widget/notification actions to the WebView. */
@CapacitorPlugin(name = "MobileQuickAction")
public class MobileQuickActionPlugin extends Plugin {
    private JSObject pending = new JSObject();

    @Override
    public void load() {
        capture(getActivity().getIntent());
    }

    @Override
    protected void handleOnNewIntent(Intent intent) {
        capture(intent);
    }

    @PluginMethod
    public void getPending(PluginCall call) {
        JSObject result = pending;
        pending = new JSObject();
        call.resolve(result);
    }

    private void capture(Intent intent) {
        if (intent == null || !HermesWidgetProvider.ACTION_NEW_TASK.equals(intent.getAction())) {
            return;
        }

        JSObject result = new JSObject();
        result.put("action", "newTask");
        pending = result;
        notifyListeners("quickAction", result, true);
    }
}
