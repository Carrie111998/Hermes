package com.nousresearch.hermesagent.mobile;

import android.Manifest;
import android.content.Context;
import android.content.Intent;
import android.net.Uri;
import android.os.Build;
import android.os.PowerManager;
import android.provider.Settings;

import com.getcapacitor.JSObject;
import com.getcapacitor.PermissionState;
import com.getcapacitor.Plugin;
import com.getcapacitor.PluginCall;
import com.getcapacitor.PluginMethod;
import com.getcapacitor.annotation.CapacitorPlugin;
import com.getcapacitor.annotation.Permission;
import com.getcapacitor.annotation.PermissionCallback;

/**
 * Deliberate, first-connection Android capability prompts.
 *
 * This plugin requests user-approved photo/video-library access and opens the
 * platform's battery-optimization exemption dialog. The latter improves the
 * odds of a resumed remote renderer reconnecting, but it does not create a
 * hidden local agent, keep a WebView immortal, or provide closed-app push.
 */
@CapacitorPlugin(
        name = "MobileCapabilities",
        permissions = {
                @Permission(
                        strings = {Manifest.permission.READ_MEDIA_IMAGES, Manifest.permission.READ_MEDIA_VIDEO},
                        alias = MobileCapabilitiesPlugin.MEDIA
                ),
                @Permission(
                        strings = {Manifest.permission.READ_EXTERNAL_STORAGE},
                        alias = MobileCapabilitiesPlugin.LEGACY_MEDIA
                )
        }
)
public class MobileCapabilitiesPlugin extends Plugin {
    static final String LEGACY_MEDIA = "legacyMedia";
    static final String MEDIA = "media";

    @PluginMethod
    public void requestMedia(PluginCall call) {
        String alias = mediaAlias();
        if (!isPermissionDeclared(alias) || getPermissionState(alias) == PermissionState.GRANTED) {
            resolveMedia(call);
            return;
        }
        requestPermissionForAlias(alias, call, "mediaPermissionsCallback");
    }

    @PermissionCallback
    public void mediaPermissionsCallback(PluginCall call) {
        resolveMedia(call);
    }

    @PluginMethod
    public void requestBackgroundReliability(PluginCall call) {
        JSObject result = new JSObject();
        if (Build.VERSION.SDK_INT < Build.VERSION_CODES.M) {
            result.put("exempt", true);
            result.put("requested", false);
            result.put("supported", false);
            call.resolve(result);
            return;
        }

        PowerManager powerManager = (PowerManager) getContext().getSystemService(Context.POWER_SERVICE);
        boolean exempt = powerManager != null
                && powerManager.isIgnoringBatteryOptimizations(getContext().getPackageName());
        result.put("exempt", exempt);
        result.put("supported", true);
        if (exempt) {
            result.put("requested", false);
            call.resolve(result);
            return;
        }

        try {
            Intent intent = new Intent(Settings.ACTION_REQUEST_IGNORE_BATTERY_OPTIMIZATIONS);
            intent.setData(Uri.parse("package:" + getContext().getPackageName()));
            getActivity().startActivity(intent);
            result.put("requested", true);
            call.resolve(result);
        } catch (Exception ignored) {
            result.put("requested", false);
            call.resolve(result);
        }
    }

    private String mediaAlias() {
        return Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU ? MEDIA : LEGACY_MEDIA;
    }

    private void resolveMedia(PluginCall call) {
        JSObject result = new JSObject();
        result.put("granted", getPermissionState(mediaAlias()) == PermissionState.GRANTED);
        result.put("supported", true);
        call.resolve(result);
    }
}
