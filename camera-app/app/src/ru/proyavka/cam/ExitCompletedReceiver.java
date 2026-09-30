package ru.proyavka.cam;

import android.content.BroadcastReceiver;
import android.content.Context;
import android.content.Intent;

/** Камера сообщает, что приложение закрыто: завершаем процесс целиком, как это делают приложения Sony. */
public class ExitCompletedReceiver extends BroadcastReceiver {
    @Override
    public void onReceive(Context context, Intent intent) {
        android.os.Process.killProcess(android.os.Process.myPid());
    }
}
