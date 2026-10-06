package ru.proyavka.cam;

import android.content.Context;
import android.os.Build;

import java.io.File;
import java.io.FileOutputStream;
import java.io.OutputStreamWriter;
import java.io.PrintWriter;
import java.io.StringWriter;
import java.io.Writer;
import java.text.SimpleDateFormat;
import java.util.Date;
import java.util.Locale;

/**
 * Журнал на карте: PROYAVKA/log.txt. Что делало приложение и что пошло не так — чтобы человек мог прислать файл, а не пересказывать
 * экран. Токен в журнал не пишется. Больше 256 КБ — старый журнал уходит в log.old.txt.
 */
public final class Log {
    private Log() {}

    private static final long MAX = 256 * 1024;
    private static final SimpleDateFormat TS = new SimpleDateFormat("yyyy-MM-dd HH:mm:ss", Locale.US);

    /** Начало сеанса: версия приложения, камера, Android. И перехват падений — стек тоже попадёт в журнал. */
    public static void start(Context ctx) {
        String ver = "?";
        try { ver = ctx.getPackageManager().getPackageInfo(ctx.getPackageName(), 0).versionName; } catch (Exception e) { /* неважно */ }
        i("---- start: Proyavka " + ver + ", " + Build.MANUFACTURER + " " + Build.MODEL + ", Android " + Build.VERSION.RELEASE
                + " (API " + Build.VERSION.SDK_INT + "), card " + Cam.dir().getPath());
        final Thread.UncaughtExceptionHandler prev = Thread.getDefaultUncaughtExceptionHandler();
        Thread.setDefaultUncaughtExceptionHandler(new Thread.UncaughtExceptionHandler() {
            @Override public void uncaughtException(Thread t, Throwable e) {
                e("crash in thread " + t.getName(), e);
                if (prev != null) prev.uncaughtException(t, e);
            }
        });
    }

    public static void i(String msg) { write(msg); }

    public static void e(String msg, Throwable t) {
        StringWriter sw = new StringWriter();
        if (t != null) t.printStackTrace(new PrintWriter(sw));
        write("ERROR " + msg + (t != null ? ": " + t + "\n" + sw.toString().trim() : ""));
    }

    private static synchronized void write(String msg) {
        try {
            File f = new File(Cam.dir(), "log.txt");
            if (f.length() > MAX) {
                File old = new File(Cam.dir(), "log.old.txt");
                old.delete();
                f.renameTo(old);
            }
            Writer w = new OutputStreamWriter(new FileOutputStream(f, true), "UTF-8");
            try {
                w.write(TS.format(new Date()) + " " + msg.replace("\n", "\r\n    ") + "\r\n");
            } finally {
                w.close();
            }
        } catch (Exception e) { /* нет карты — журнала не будет, работе это не мешает */ }
    }
}
