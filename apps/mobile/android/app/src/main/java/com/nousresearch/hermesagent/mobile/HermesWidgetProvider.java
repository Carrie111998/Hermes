package com.nousresearch.hermesagent.mobile;

import android.app.PendingIntent;
import android.appwidget.AppWidgetManager;
import android.appwidget.AppWidgetProvider;
import android.content.Context;
import android.content.Intent;
import android.widget.RemoteViews;

/**
 * A deliberately small home-screen entry point. It never sends hidden work or
 * starts capture: Open Hermes resumes the client, and New task opens a visible
 * composer so the person can review and press Send themselves.
 */
public class HermesWidgetProvider extends AppWidgetProvider {
    public static final String ACTION_NEW_TASK = "com.nousresearch.hermesagent.mobile.NEW_TASK";

    @Override
    public void onUpdate(Context context, AppWidgetManager manager, int[] appWidgetIds) {
        for (int widgetId : appWidgetIds) {
            RemoteViews views = new RemoteViews(context.getPackageName(), R.layout.hermes_widget);
            views.setOnClickPendingIntent(R.id.hermes_widget_open, activityIntent(context, Intent.ACTION_MAIN, 100));
            views.setOnClickPendingIntent(R.id.hermes_widget_new_task, activityIntent(context, ACTION_NEW_TASK, 101));
            manager.updateAppWidget(widgetId, views);
        }
    }

    private PendingIntent activityIntent(Context context, String action, int requestCode) {
        Intent intent = new Intent(context, MainActivity.class)
                .setAction(action)
                .addFlags(Intent.FLAG_ACTIVITY_CLEAR_TOP | Intent.FLAG_ACTIVITY_SINGLE_TOP);
        return PendingIntent.getActivity(
                context,
                requestCode,
                intent,
                PendingIntent.FLAG_UPDATE_CURRENT | PendingIntent.FLAG_IMMUTABLE
        );
    }
}
