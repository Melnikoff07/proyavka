package ru.proyavka.cam;

import android.app.Activity;
import android.app.AlertDialog;
import android.content.BroadcastReceiver;
import android.content.Context;
import android.content.DialogInterface;
import android.content.Intent;
import android.content.IntentFilter;
import android.graphics.Color;
import android.graphics.Typeface;
import android.net.wifi.ScanResult;
import android.net.wifi.WifiConfiguration;
import android.net.wifi.WifiManager;
import android.os.Bundle;
import android.os.Handler;
import android.os.SystemClock;
import android.text.InputType;
import android.view.KeyEvent;
import android.view.View;
import android.view.ViewGroup;
import android.view.WindowManager;
import android.widget.AdapterView;
import android.widget.ArrayAdapter;
import android.widget.EditText;
import android.widget.LinearLayout;
import android.widget.ListView;
import android.widget.TextView;

import java.io.IOException;
import java.util.ArrayList;
import java.util.Collections;
import java.util.Comparator;
import java.util.HashMap;
import java.util.List;
import java.util.Map;

/**
 * Экран Wi-Fi: сети вокруг и сохранённые. Выбрал сеть → ввёл пароль → приложение проверяет, что подключилось,
 * и только тогда сохраняет её на карту (PROYAVKA/wifi.txt). Дальше камера подключается к ней сама.
 */
public class WifiActivity extends Activity {
    private static final int SC_MENU = 514, SC_DELETE = 595;
    private static final long CONNECT_WAIT_MS = 25000;

    private final Handler ui = new Handler();
    private WifiManager wifi;
    private TextView status;
    private ListView list;
    private final List<Row> rows = new ArrayList<Row>();
    private ArrayAdapter<Row> adapter;
    private BroadcastReceiver scanReceiver;
    private volatile boolean connecting;
    private boolean dialogOpen;

    private static final class Row {
        final String ssid;
        final boolean saved, open;
        final int level;   // 0..4, -1 — сети нет рядом
        Row(String ssid, boolean saved, boolean open, int level) {
            this.ssid = ssid; this.saved = saved; this.open = open; this.level = level;
        }
        @Override public String toString() {
            String bars = level < 0 ? Cam.L("не рядом", "not nearby") : "▂▄▆█".substring(0, Math.max(1, level));
            return (saved ? "✓ " : "   ") + ssid + "   " + bars + (open ? Cam.L("  (открытая)", "  (open)") : "");
        }
    }

    private final Runnable rescan = new Runnable() {
        @Override public void run() {
            if (wifi.isWifiEnabled()) wifi.startScan();
            ui.postDelayed(this, 10000);
        }
    };

    @Override
    protected void onCreate(Bundle b) {
        super.onCreate(b);
        wifi = (WifiManager) getSystemService(Context.WIFI_SERVICE);

        LinearLayout root = new LinearLayout(this);
        root.setOrientation(LinearLayout.VERTICAL);
        root.setBackgroundColor(Color.BLACK);
        root.setPadding(40, 20, 40, 16);
        TextView title = Cam.text(this, 26, Color.WHITE);
        title.setTypeface(Typeface.DEFAULT_BOLD);
        title.setText("Wi-Fi");
        status = Cam.text(this, 16, Cam.GRAY);
        status.setPadding(0, 4, 0, 10);
        list = new ListView(this);
        list.setBackgroundColor(Color.BLACK);
        list.setCacheColorHint(Color.BLACK);
        list.setSelector(new android.graphics.drawable.ColorDrawable(Color.rgb(230, 120, 20)));
        adapter = new ArrayAdapter<Row>(this, android.R.layout.simple_list_item_1, rows) {
            @Override public View getView(int pos, View v, ViewGroup parent) {
                TextView t = (TextView) super.getView(pos, v, parent);
                t.setTextColor(Color.WHITE);
                t.setTextSize(19);
                return t;
            }
        };
        list.setAdapter(adapter);
        list.setOnItemClickListener(new AdapterView.OnItemClickListener() {
            @Override public void onItemClick(AdapterView<?> p, View v, int pos, long id) { choose(rows.get(pos)); }
        });
        TextView hint = Cam.text(this, 14, Cam.GRAY);
        hint.setText(Cam.L("Колесо — выбрать, центр — открыть, MENU — назад", "Wheel — select, center — open, MENU — back"));
        hint.setPadding(0, 8, 0, 0);

        root.addView(title);
        root.addView(status);
        root.addView(list, new LinearLayout.LayoutParams(LinearLayout.LayoutParams.MATCH_PARENT, 0, 1));
        root.addView(hint);
        setContentView(root);
        getWindow().addFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON);

        scanReceiver = new BroadcastReceiver() {
            @Override public void onReceive(Context c, Intent i) { rebuild(); }
        };
    }

    @Override
    protected void onResume() {
        super.onResume();
        if (Cam.exitAll) { finish(); return; }
        Cam.notifyAppInfo(this);
        registerReceiver(scanReceiver, new IntentFilter(WifiManager.SCAN_RESULTS_AVAILABLE_ACTION));
        Cam.enableWifi(wifi);
        status.setText(Cam.L("Ищу сети…", "Searching…"));
        rebuild();
        ui.post(rescan);
        list.requestFocus();
    }

    @Override
    protected void onPause() {
        super.onPause();
        ui.removeCallbacksAndMessages(null);
        try { unregisterReceiver(scanReceiver); } catch (IllegalArgumentException e) { /* уже снят */ }
        if (!isFinishing()) {   // не «назад», а камера ушла в съёмку или выключается — закрываем всё
            Cam.exitAll = true;
            finish();
        }
    }

    @Override
    public boolean onKeyDown(int keyCode, KeyEvent e) {
        int sc = e.getScanCode();
        if (keyCode == KeyEvent.KEYCODE_MENU || sc == SC_MENU || keyCode == KeyEvent.KEYCODE_BACK) {
            if (!connecting) finish();
            return true;
        }
        if (sc == SC_DELETE && !connecting) {   // корзина на сохранённой сети — забыть
            int pos = list.getSelectedItemPosition();
            if (pos >= 0 && pos < rows.size() && rows.get(pos).saved) confirmForget(rows.get(pos).ssid);
            return true;
        }
        return super.onKeyDown(keyCode, e);
    }

    /** Список: сохранённые наверху (по порядку приоритета), потом остальные по силе сигнала. */
    private void rebuild() {
        if (connecting) return;
        List<Cam.Net> saved = savedNets();
        Map<String, ScanResult> seen = new HashMap<String, ScanResult>();
        List<ScanResult> scan = wifi.getScanResults();
        if (scan != null) for (ScanResult r : scan) {
            if (r.SSID == null || r.SSID.length() == 0) continue;
            ScanResult old = seen.get(r.SSID);
            if (old == null || r.level > old.level) seen.put(r.SSID, r);
        }
        List<Row> out = new ArrayList<Row>();
        for (Cam.Net n : saved) {
            ScanResult r = seen.remove(n.ssid);
            out.add(new Row(n.ssid, true, n.pass.length() == 0, r == null ? -1 : level(r)));
        }
        List<ScanResult> rest = new ArrayList<ScanResult>(seen.values());
        Collections.sort(rest, new Comparator<ScanResult>() {
            @Override public int compare(ScanResult a, ScanResult b) { return b.level - a.level; }
        });
        for (ScanResult r : rest) out.add(new Row(r.SSID, false, isOpen(r), level(r)));

        int sel = list.getSelectedItemPosition();
        rows.clear();
        rows.addAll(out);
        adapter.notifyDataSetChanged();
        if (sel >= 0 && sel < rows.size()) list.setSelection(sel);
        String cur = Cam.currentSsid(this, wifi);
        status.setText((cur != null ? Cam.L("Подключено: ", "Connected: ") + cur + ".  " : "") +
                (saved.isEmpty() ? Cam.L("Выбери сеть — телефон с точкой доступа или дом", "Pick a network — phone hotspot or home")
                        : Cam.L("✓ — сохранённые сети", "✓ — saved networks")));
    }

    private static int level(ScanResult r) { return WifiManager.calculateSignalLevel(r.level, 5); }

    private static boolean isOpen(ScanResult r) {
        String c = r.capabilities == null ? "" : r.capabilities;
        return !(c.contains("WPA") || c.contains("WEP") || c.contains("PSK") || c.contains("EAP"));
    }

    private List<Cam.Net> savedNets() {
        try {
            return Cam.networks(Cam.config(this));
        } catch (IOException e) {
            return Cam.ownNetworks();
        }
    }

    // ---------- действия ----------

    private void choose(final Row r) {
        if (connecting) return;
        if (!r.saved) {
            if (r.open) connect(r.ssid, "");
            else askPassword(r.ssid, "");
            return;
        }
        dialog(new AlertDialog.Builder(this).setTitle(r.ssid)
                .setItems(new String[] {Cam.L("Подключиться", "Connect"), Cam.L("Ввести пароль заново", "Re-enter password"), Cam.L("Забыть сеть", "Forget network")},
                        new DialogInterface.OnClickListener() {
                            @Override public void onClick(DialogInterface d, int which) {
                                dialogOpen = false;
                                if (which == 0) connect(r.ssid, passOf(r.ssid));
                                else if (which == 1) askPassword(r.ssid, "");
                                else confirmForget(r.ssid);
                            }
                        }));
    }

    private String passOf(String ssid) {
        List<Cam.Net> l = savedNets();
        int i = Cam.find(l, ssid);
        return i < 0 ? "" : l.get(i).pass;
    }

    private void askPassword(final String ssid, String prefill) {
        final EditText e = new EditText(this);
        e.setSingleLine(true);
        // пароль видно: вводить его колесом камеры и так непросто, пусть хоть проверить можно будет
        e.setInputType(InputType.TYPE_CLASS_TEXT | InputType.TYPE_TEXT_VARIATION_VISIBLE_PASSWORD);
        e.setText(prefill);
        e.setSelection(prefill.length());
        dialog(new AlertDialog.Builder(this).setTitle(Cam.L("Пароль для ", "Password for ") + ssid).setView(e)
                .setPositiveButton(Cam.L("Подключить", "Connect"), new DialogInterface.OnClickListener() {
                    @Override public void onClick(DialogInterface d, int w) {
                        dialogOpen = false;
                        String p = e.getText().toString();
                        if (p.length() < 8) {
                            status.setText(Cam.L("В пароле Wi-Fi минимум 8 символов", "A Wi-Fi password has at least 8 characters"));
                            askPassword(ssid, p);
                        } else {
                            connect(ssid, p);
                        }
                    }
                })
                .setNegativeButton(Cam.L("Отмена", "Cancel"), null));
        e.requestFocus();
    }

    private void confirmForget(final String ssid) {
        dialog(new AlertDialog.Builder(this).setTitle(Cam.L("Забыть ", "Forget ") + ssid + "?")
                .setPositiveButton(Cam.L("Забыть", "Forget"), new DialogInterface.OnClickListener() {
                    @Override public void onClick(DialogInterface d, int w) {
                        dialogOpen = false;
                        forget(ssid);
                    }
                })
                .setNegativeButton(Cam.L("Отмена", "Cancel"), null));
    }

    private void dialog(AlertDialog.Builder b) {
        dialogOpen = true;
        AlertDialog d = b.create();
        d.setOnDismissListener(new DialogInterface.OnDismissListener() {
            @Override public void onDismiss(DialogInterface di) { dialogOpen = false; }
        });
        d.getWindow().setSoftInputMode(WindowManager.LayoutParams.SOFT_INPUT_STATE_ALWAYS_VISIBLE);
        d.show();
    }

    private void forget(String ssid) {
        List<Cam.Net> own = Cam.ownNetworks();
        int i = Cam.find(own, ssid);
        if (i >= 0) {
            own.remove(i);
            try { Cam.saveOwn(own); } catch (IOException e) { status.setText(Cam.L("Не записать на карту: ", "Can't write to the card: ") + e.getMessage()); return; }
        }
        WifiConfiguration c = Cam.configured(wifi, ssid);
        if (c != null) { wifi.removeNetwork(c.networkId); wifi.saveConfiguration(); }
        boolean fromConfig = Cam.find(savedNets(), ssid) >= 0;
        status.setText(fromConfig ? ssid + Cam.L(" задана в config.txt — убери её там (мастер установки)", " is set in config.txt — remove it there (setup wizard)")
                : Cam.L("Забыта: ", "Forgotten: ") + ssid);
        rebuild();
    }

    /** Подключиться и дождаться результата; сохранить на карту только если получилось. */
    private void connect(final String ssid, final String pass) {
        connecting = true;
        status.setText(Cam.L("Подключаюсь к ", "Connecting to ") + ssid + "…");
        Cam.autoPowerOff(this, false);
        new Thread(new Runnable() { @Override public void run() {
            final boolean existed = Cam.configured(wifi, ssid) != null;
            int id = Cam.put(wifi, new Cam.Net(ssid, pass), 200);
            boolean ok = false;
            if (id >= 0) {
                wifi.enableNetwork(id, true);   // true — только к ней, остальные временно отключаются
                wifi.reconnect();
                long until = SystemClock.elapsedRealtime() + CONNECT_WAIT_MS;
                while (SystemClock.elapsedRealtime() < until) {
                    if (ssid.equals(Cam.currentSsid(WifiActivity.this, wifi))) { ok = true; break; }
                    SystemClock.sleep(500);
                }
            }
            String msg;
            if (ok) {
                List<Cam.Net> own = Cam.ownNetworks();
                int i = Cam.find(own, ssid);
                if (i >= 0) own.remove(i);
                own.add(0, new Cam.Net(ssid, pass));   // последняя подключённая — самая приоритетная
                try {
                    Cam.saveOwn(own);
                    msg = Cam.L("Сохранено: ", "Saved: ") + ssid + Cam.L(". Камера будет подключаться к ней сама", ". The camera will connect to it by itself");
                } catch (IOException e) {
                    msg = Cam.L("Подключилось, но не записалось на карту: ", "Connected, but could not write to the card: ") + e.getMessage();
                }
            } else {
                if (id >= 0 && !existed) wifi.removeNetwork(id);
                msg = Cam.L("Не подключилось к ", "Could not connect to ") + ssid
                        + (pass.length() > 0 ? Cam.L(". Проверь пароль", ". Check the password") : "");
            }
            // вернуть в работу остальные сети
            List<WifiConfiguration> all = wifi.getConfiguredNetworks();
            if (all != null) for (WifiConfiguration c : all) wifi.enableNetwork(c.networkId, false);
            wifi.saveConfiguration();
            final String m = msg;
            final boolean success = ok;
            ui.post(new Runnable() { @Override public void run() {
                connecting = false;
                Cam.autoPowerOff(WifiActivity.this, true);
                rebuild();
                status.setText(m);
                if (!success && pass.length() > 0) askPassword(ssid, pass);
            } });
        } }, "wifi-connect").start();
    }
}
