package ru.proyavka.cam;

import java.io.InputStream;
import java.net.InetAddress;
import java.net.Socket;
import java.security.KeyStore;
import java.security.cert.Certificate;
import java.security.cert.CertificateFactory;
import java.util.ArrayList;
import java.util.Arrays;
import java.util.List;

import javax.net.ssl.SSLContext;
import javax.net.ssl.SSLSocket;
import javax.net.ssl.SSLSocketFactory;
import javax.net.ssl.TrustManagerFactory;

/**
 * HTTPS для камер: доверяем только корням Let's Encrypt, лежащим в приложении (в системном хранилище камеры их нет).
 * Android 4.1: включаем TLS 1.2 — он есть, но выключен. Android 2.3: TLS 1.2 нет вовсе, остаётся то, что есть (TLS 1.0) —
 * для таких камер на сервере отдельный порт.
 */
public final class Tls {
    private Tls() {}

    public static SSLSocketFactory factory(List<InputStream> pems) throws Exception {
        CertificateFactory cf = CertificateFactory.getInstance("X.509");
        KeyStore ks = KeyStore.getInstance(KeyStore.getDefaultType());
        ks.load(null, null);
        int i = 0;
        for (InputStream in : pems) {
            try {
                for (Certificate c : cf.generateCertificates(in)) ks.setCertificateEntry("ca" + (i++), c);
            } finally {
                in.close();
            }
        }
        TrustManagerFactory tmf = TrustManagerFactory.getInstance(TrustManagerFactory.getDefaultAlgorithm());
        tmf.init(ks);
        SSLContext ctx = SSLContext.getInstance("TLS");
        ctx.init(null, tmf.getTrustManagers(), null);
        return new Tls12Factory(ctx.getSocketFactory());
    }

    static final class Tls12Factory extends SSLSocketFactory {
        private final SSLSocketFactory d;

        Tls12Factory(SSLSocketFactory d) { this.d = d; }

        private Socket fix(Socket s) {
            if (s instanceof SSLSocket) {
                SSLSocket ss = (SSLSocket) s;
                List<String> want = new ArrayList<String>();
                List<String> sup = Arrays.asList(ss.getSupportedProtocols());
                for (String p : new String[] {"TLSv1.2", "TLSv1.1"}) if (sup.contains(p)) want.add(p);
                if (!want.isEmpty()) ss.setEnabledProtocols(want.toArray(new String[0]));
            }
            return s;
        }

        @Override public String[] getDefaultCipherSuites() { return d.getDefaultCipherSuites(); }
        @Override public String[] getSupportedCipherSuites() { return d.getSupportedCipherSuites(); }
        @Override public Socket createSocket() throws java.io.IOException { return fix(d.createSocket()); }
        @Override public Socket createSocket(Socket s, String h, int p, boolean ac) throws java.io.IOException { return fix(d.createSocket(s, h, p, ac)); }
        @Override public Socket createSocket(String h, int p) throws java.io.IOException { return fix(d.createSocket(h, p)); }
        @Override public Socket createSocket(String h, int p, InetAddress la, int lp) throws java.io.IOException { return fix(d.createSocket(h, p, la, lp)); }
        @Override public Socket createSocket(InetAddress h, int p) throws java.io.IOException { return fix(d.createSocket(h, p)); }
        @Override public Socket createSocket(InetAddress a, int p, InetAddress la, int lp) throws java.io.IOException { return fix(d.createSocket(a, p, la, lp)); }
    }
}
