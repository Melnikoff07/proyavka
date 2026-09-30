package ru.proyavka.cam;

import android.app.Activity;
import android.content.Context;
import android.content.Intent;
import android.graphics.Color;
import android.graphics.Typeface;
import android.graphics.drawable.ColorDrawable;
import android.graphics.drawable.StateListDrawable;
import android.net.wifi.WifiManager;
import android.os.Bundle;
import android.os.Environment;
import android.os.Handler;
import android.os.SystemClock;
import android.view.Gravity;
import android.view.KeyEvent;
import android.view.View;
import android.widget.Button;
import android.widget.LinearLayout;
import android.widget.ProgressBar;
import android.widget.TextView;

import java.io.File;
import java.io.IOException;
import java.util.ArrayList;
import java.util.List;
import java.util.Properties;

/**
 * Главный экран: «Отправить новые», «Wi-Fi», «Выход». Колесо/кнопки — выбор, центральная кнопка — нажать.
 * Отправка: ищет новые JPEG, включает Wi-Fi, подключается к сохранённой сети, отправляет, показывает итог.
 */
public class MainActivity extends Activity {
    // сканкоды кнопок камеры (com.sony.scalar.sysutil.ScalarInput)
    private static final int SC_DELETE = 595, SC_SK2 = 513, SC_MENU = 514, SC_SK1 = 229;

    private static final long WIFI_WAIT_MS = 60000;
    private static final long AUTO_EXIT_MS = 20000;
    private static final int TRIES = 3;

    private final Handler ui = new Handler();
    private TextView status, detail;
    private ProgressBar bar;
    private LinearLayout buttons;
    private Button sendBtn;
    private WifiManager wifi;
    private volatile WifiManager.WifiLock wifiLock;
    private volatile Uploader job;
    private volatile boolean busy;
    private boolean childOpen;
    private final Runnable autoExit = new Runnable() { @Override public void run() { finish(); } };

    @Override
    protected void onCreate(Bundle b) {
        super.onCreate(b);
        wifi = (WifiManager) getSystemService(Context.WIFI_SERVICE);
        Cam.exitAll = false;
        Cam.initLang(this);

        LinearLayout root = new LinearLayout(this);
        root.setOrientation(LinearLayout.VERTICAL);
        root.setBackgroundColor(Color.BLACK);
        root.setPadding(40, 24, 40, 24);
        root.setGravity(Gravity.CENTER_VERTICAL);

        TextView title = Cam.text(this, 30, Color.WHITE);
        title.setTypeface(Typeface.DEFAULT_BOLD);
        title.setText(Cam.L("Проявка", "Proyavka"));
        status = Cam.text(this, 22, Color.WHITE);
        status.setPadding(0, 14, 0, 8);
        bar = new ProgressBar(this, null, android.R.attr.progressBarStyleHorizontal);
        bar.setMax(1000);
        bar.setVisibility(View.GONE);
        detail = Cam.text(this, 17, Cam.GRAY);
        detail.setPadding(0, 6, 0, 14);

        buttons = new LinearLayout(this);
        buttons.setOrientation(LinearLayout.VERTICAL);
        sendBtn = button(this, Cam.L("Отправить новые", "Send new"), new View.OnClickListener() {
            @Override public void onClick(View v) { startSend(); }
        });
        buttons.addView(sendBtn);
        buttons.addView(button(this, "Wi-Fi", new View.OnClickListener() {
            @Override public void onClick(View v) {
                cancelAutoExit();
                childOpen = true;
                startActivity(new Intent(MainActivity.this, WifiActivity.class));
            }
        }));
        buttons.addView(button(this, Cam.L("Выход", "Exit"), new View.OnClickListener() {
            @Override public void onClick(View v) { finish(); }
        }));

        root.addView(title);
        root.addView(status);
        root.addView(bar, new LinearLayout.LayoutParams(LinearLayout.LayoutParams.MATCH_PARENT, 16));
        root.addView(detail);
        root.addView(buttons);
        setContentView(root);
        getWindow().addFlags(android.view.WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON);
    }

    /** Кнопка, заметная на камере: выбранная — оранжевая. */
    static Button button(Context ctx, String label, View.OnClickListener l) {
        Button b = new Button(ctx);
        b.setText(label);
        b.setTextSize(20);
        b.setTextColor(Color.WHITE);
        b.setGravity(Gravity.CENTER_VERTICAL | Gravity.LEFT);
        b.setPadding(24, 10, 24, 10);
        StateListDrawable bg = new StateListDrawable();
        bg.addState(new int[] {android.R.attr.state_pressed}, new ColorDrawable(Color.rgb(255, 170, 60)));
        bg.addState(new int[] {android.R.attr.state_focused}, new ColorDrawable(Color.rgb(230, 120, 20)));
        bg.addState(new int[] {}, new ColorDrawable(Color.rgb(40, 40, 40)));
        b.setBackgroundDrawable(bg);
        b.setFocusable(true);
        b.setOnClickListener(l);
        LinearLayout.LayoutParams lp = new LinearLayout.LayoutParams(LinearLayout.LayoutParams.MATCH_PARENT,
                LinearLayout.LayoutParams.WRAP_CONTENT);
        lp.setMargins(0, 4, 0, 4);
        b.setLayoutParams(lp);
        return b;
    }

    @Override
    protected void onResume() {
        super.onResume();
        if (Cam.exitAll) { finish(); return; }
        childOpen = false;
        Cam.notifyAppInfo(this);
        if (!busy) {
            refresh();
            sendBtn.requestFocus();
        }
    }

    @Override
    protected void onPause() {
        super.onPause();
        cancelAutoExit();
        if (childOpen) return;   // открыли свой экран Wi-Fi — это не выход
        // на паузе без своего экрана = камера ушла в съёмку, выключается или пользователь вышел
        Uploader j = job;
        if (j != null) j.cancel();
        releaseWifiLock();
        Cam.restoreWifi(wifi);
        Cam.autoPowerOff(this, true);
        Cam.exitAll = true;
        finish();
    }

    @Override
    public boolean onKeyDown(int keyCode, KeyEvent e) {
        cancelAutoExit();
        int sc = e.getScanCode();
        if (busy && (sc == SC_DELETE || sc == SC_SK2 || sc == SC_MENU || sc == SC_SK1 || keyCode == KeyEvent.KEYCODE_BACK
                || keyCode == KeyEvent.KEYCODE_MENU)) {
            Uploader j = job;
            if (j != null) j.cancel();
            show(Cam.L("Отменяю…", "Cancelling…"), null, -1);
            return true;
        }
        if (!busy && (keyCode == KeyEvent.KEYCODE_MENU || sc == SC_MENU)) {   // как в Tweak: MENU — выход
            finish();
            return true;
        }
        return super.onKeyDown(keyCode, e);
    }

    private void cancelAutoExit() {
        ui.removeCallbacks(autoExit);
    }

    /** Что на карте и какие сети сохранены — чтобы сразу было видно, всё ли готово. */
    private void refresh() {
        String st, dt;
        try {
            Properties cfg = Cam.config(this);
            List<Cam.Net> nets = Cam.networks(cfg);
            List<String> names = new ArrayList<String>();
            for (Cam.Net n : nets) names.add(n.ssid);
            Uploader u = uploader(cfg);
            if (u == null) {
                st = Cam.L("Нет настроек сервера", "No server settings");
                dt = Cam.L("Положи на карту файл PROYAVKA/config.txt — его пришлёт бот по команде /camera",
                        "Put PROYAVKA/config.txt on the card — the bot sends it with /camera");
            } else {
                int n = u.findNew(new File(Environment.getExternalStorageDirectory(), "DCIM")).size();
                st = n == 0 ? Cam.L("Новых кадров нет", "No new frames") : Cam.L("Новых кадров: ", "New frames: ") + n;
                dt = nets.isEmpty() ? Cam.L("Wi-Fi не настроен — зайди в «Wi-Fi» и выбери сеть", "Wi-Fi is not set up — open Wi-Fi and pick a network")
                        : "Wi-Fi: " + Cam.join(names);
            }
        } catch (Exception e) {
            st = Cam.L("Ошибка", "Error");
            dt = String.valueOf(e.getMessage());
        }
        status.setText(st);
        detail.setText(dt);
    }

    private Uploader uploader(Properties cfg) throws Exception {
        String url = cfg.getProperty("url", "").trim(), token = cfg.getProperty("token", "").trim();
        if (url.length() == 0 || token.length() == 0) return null;
        String model = android.os.Build.MODEL == null ? "" : android.os.Build.MODEL.replaceAll("[^A-Za-z0-9-]", "");
        return new Uploader(url, token, cfg.getProperty("prefix", model.length() > 0 ? model + "_" : "cam_").trim(),
                Tls.factory(Cam.certs(this)), new File(Cam.dir(), "sent.txt"));
    }

    // ---------- отправка (в отдельном потоке) ----------

    private void startSend() {
        if (busy) return;
        busy = true;
        buttons.setVisibility(View.GONE);
        bar.setProgress(0);
        bar.setVisibility(View.VISIBLE);
        Cam.autoPowerOff(this, false);
        new Thread(new Runnable() { @Override public void run() { work(); } }, "upload").start();
    }

    private void work() {
        String result;
        try {
            result = send();
        } catch (Exception e) {
            result = Cam.L("Ошибка: ", "Error: ") + e.getMessage();
        }
        final String r = result;
        releaseWifiLock();
        Cam.restoreWifi(wifi);
        Cam.autoPowerOff(this, true);
        job = null;
        ui.post(new Runnable() { @Override public void run() {
            busy = false;
            bar.setVisibility(View.GONE);
            status.setText(r);
            detail.setText(Cam.L("Закроется само через 20 секунд", "Closes by itself in 20 seconds"));
            buttons.setVisibility(View.VISIBLE);
            sendBtn.requestFocus();
            ui.postDelayed(autoExit, AUTO_EXIT_MS);
        } });
    }

    private String send() throws Exception {
        Properties cfg = Cam.config(this);
        final Uploader u = uploader(cfg);
        if (u == null) return Cam.L("Нет настроек. Положи на карту PROYAVKA/config.txt — его пришлёт бот по /camera",
                "No settings. Put PROYAVKA/config.txt on the card — the bot sends it with /camera");
        job = u;

        show(Cam.L("Ищу новые кадры", "Looking for new frames"), "", 0);
        List<File> files = u.findNew(new File(Environment.getExternalStorageDirectory(), "DCIM"));
        if (files.isEmpty()) return Cam.L("Новых кадров нет", "No new frames");
        List<Cam.Net> nets = Cam.networks(cfg);
        if (nets.isEmpty()) return Cam.L("Wi-Fi не настроен — зайди в «Wi-Fi» и выбери сеть", "Wi-Fi is not set up — open Wi-Fi and pick a network");
        long all = 0;
        for (File f : files) all += f.length();
        final int count = files.size();

        // сеть
        Cam.enableWifi(wifi);
        show(Cam.L("Подключаюсь к Wi-Fi", "Connecting to Wi-Fi"),
                Cam.count(count, "кадр", "кадра", "кадров", "frame", "frames") + Cam.L(" ждут отправки", " waiting to be sent"), 0);
        long until = SystemClock.elapsedRealtime() + WIFI_WAIT_MS;
        while (wifi.getWifiState() != WifiManager.WIFI_STATE_ENABLED) {
            if (u.isCancelled()) return Cam.L("Отменено", "Cancelled");
            if (SystemClock.elapsedRealtime() > until) return Cam.L("Wi-Fi не включается", "Wi-Fi does not turn on");
            SystemClock.sleep(300);
        }
        Cam.applyAll(wifi, nets);
        while (!Cam.connected(this)) {
            if (u.isCancelled()) return Cam.L("Отменено", "Cancelled");
            if (SystemClock.elapsedRealtime() > until) {
                List<String> names = new ArrayList<String>();
                for (Cam.Net n : nets) names.add(n.ssid);
                return Cam.L("Нет Wi-Fi. Не вижу сетей: ", "No Wi-Fi. Can't see: ") + Cam.join(names);
            }
            SystemClock.sleep(500);
        }
        wifiLock = wifi.createWifiLock(WifiManager.WIFI_MODE_FULL_HIGH_PERF, "proyavka");
        wifiLock.acquire();   // без энергосбережения Wi-Fi на время отправки
        show(Cam.L("Проверяю сервер", "Checking the server"), Cam.L("Сеть: ", "Network: ") + Cam.currentSsid(this, wifi), 0);
        u.ping();

        // отправка
        int ok = 0;
        long before = 0;
        String lastErr = null;
        for (int i = 0; i < count; i++) {
            final File f = files.get(i);
            show(Cam.L("Кадр ", "Frame ") + (i + 1) + Cam.L(" из ", " of ") + count, f.getName(), -1);
            boolean sent = false;
            for (int t = 1; t <= TRIES && !sent; t++) {
                if (u.isCancelled()) break;
                try {
                    final long t0 = SystemClock.elapsedRealtime();
                    u.upload(f, before, all, new Uploader.Listener() {
                        @Override public void fileStarted(int index, int c, File file) {}
                        @Override public void progress(long fd, long fs, long ad, long as) {
                            long ms = Math.max(1, SystemClock.elapsedRealtime() - t0);
                            String speed = String.format(Cam.L("%.1f МБ/с", "%.1f MB/s"), fd / 1048576.0 / (ms / 1000.0));
                            show(null, f.getName() + " · " + (fd * 100 / Math.max(1, fs)) + "% · " + speed,
                                    (int) (ad * 1000 / Math.max(1, as)));
                        }
                    });
                    u.markSent(f);
                    sent = true;
                } catch (IOException e) {
                    lastErr = e.getMessage();
                    if (!u.isCancelled() && t < TRIES) {
                        show(null, f.getName() + ": " + lastErr + Cam.L(", ещё попытка", ", retrying"), -1);
                        SystemClock.sleep(2000L * t);
                    }
                }
            }
            before += f.length();
            if (sent) ok++;
            if (u.isCancelled()) break;
        }
        if (u.isCancelled()) return Cam.L("Отменено. Отправлено ", "Cancelled. Sent ") + ok + Cam.L(" из ", " of ") + count;
        if (ok == count) return Cam.L("Готово: ", "Done: ")
                + Cam.count(ok, "кадр отправлен", "кадра отправлено", "кадров отправлено", "frame sent", "frames sent");
        return Cam.L("Отправлено ", "Sent ") + ok + Cam.L(" из ", " of ") + count + (lastErr != null ? ". " + lastErr : "");
    }

    private synchronized void releaseWifiLock() {
        if (wifiLock != null && wifiLock.isHeld()) wifiLock.release();
    }

    /** null — не трогать строку; progress -1 — не трогать полосу. */
    private void show(final String s, final String d, final int progress) {
        ui.post(new Runnable() { @Override public void run() {
            if (s != null) status.setText(s);
            if (d != null) detail.setText(d);
            if (progress >= 0) bar.setProgress(progress);
        } });
    }
}
