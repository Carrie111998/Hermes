package com.nousresearch.hermesagent.mobile;

import android.content.Context;
import android.media.AudioManager;
import android.os.Build;

import com.getcapacitor.Plugin;
import com.getcapacitor.PluginCall;
import com.getcapacitor.PluginMethod;
import com.getcapacitor.annotation.CapacitorPlugin;

/**
 * Keeps ordinary media/TTS output on the device speaker. This does not acquire
 * the microphone, start a call, or persist a telephony route. A future call
 * surface must make earpiece/Bluetooth selection an explicit user decision.
 */
@CapacitorPlugin(name = "MobileAudioRoute")
public class MobileAudioRoutePlugin extends Plugin {
    @PluginMethod
    public void useSpeaker(PluginCall call) {
        AudioManager audio = (AudioManager) getContext().getSystemService(Context.AUDIO_SERVICE);
        if (audio == null) {
            call.reject("Audio route unavailable");
            return;
        }

        audio.setMode(AudioManager.MODE_NORMAL);
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.S) {
            audio.clearCommunicationDevice();
        }
        audio.setSpeakerphoneOn(true);
        call.resolve();
    }
}
