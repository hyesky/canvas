import http.server, os, socketserver, urllib.request, ssl, sqlite3, json
os.chdir(os.path.expanduser("~/canvas"))
DB = os.path.join(os.path.expanduser("~/canvas"), "config.db")
UPSTREAM_PROXY = "http://70.39.197.194:51080"   # ponytail: github reachable only via this proxy
_ctx = ssl._create_unverified_context()  # ponytail: local dev; python has no CA bundle (match curl)

def init_db():
    c = sqlite3.connect(DB)
    c.execute("CREATE TABLE IF NOT EXISTS kv(key TEXT PRIMARY KEY, value TEXT)")
    c.commit(); c.close()
init_db()

def kv_get(prefix):
    c = sqlite3.connect(DB)
    rows = c.execute("SELECT key,value FROM kv WHERE key LIKE ?", (prefix + "%",)).fetchall()
    c.close(); return dict(rows)

def kv_put(items):
    c = sqlite3.connect(DB)
    c.executemany("INSERT INTO kv(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                  list(items.items()))
    c.commit(); c.close()

# Injected before the app's deferred module: restore stored config into localStorage,
# then capture any infinite-canvas:* write and push it to SQLite (debounced).
RESTORE_SHIM = """<script>
(function(){
  var RESTORE = %s;
  var orig = Storage.prototype.setItem;
  var pending = {};
  function flush(){ var p=pending; pending={}; if(!Object.keys(p).length) return;
    fetch('/api/kv',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({items:p})}); }
  Object.keys(RESTORE).forEach(function(k){ orig.call(localStorage, k, RESTORE[k]); });
  Storage.prototype.setItem = function(k,v){
    var key=String(k);
    if(key.indexOf('infinite-canvas:')===0){ pending[key]=String(v); clearTimeout(window.__kvT); window.__kvT=setTimeout(flush,600); }
    return orig.call(this,k,v);
  };
})();
</script>"""

class H(http.server.SimpleHTTPRequestHandler):
    def do_POST(self):
        if self.path.split('?')[0] == '/api/kv':
            n = int(self.headers.get('Content-Length', '0') or 0)
            try:
                data = json.loads(self.rfile.read(n) or b'{}')
            except Exception:
                data = {}
            kv_put(data.get('items', {}))
            self.send_response(200); self.send_header('Content-Type', 'application/json'); self.end_headers()
            self.wfile.write(b'{"ok":true}')
        else:
            self.send_response(404); self.end_headers()

    def do_GET(self):
        p = self.path.split('?')[0]
        if p.startswith('/ghproxy/'):
            return self._proxy(p[len('/ghproxy/'):])
        f = p.lstrip('/')
        if not f:
            f = 'index.html'
        # serve index.html with the config shim for '/' & SPA fallback routes
        if (os.path.isfile(f) and f == 'index.html') or not os.path.isfile(f):
            return self._serve_index()
        return super().do_GET()

    def _serve_index(self):
        html = open('index.html', 'r', encoding='utf-8').read()
        shim = RESTORE_SHIM % json.dumps(kv_get('infinite-canvas:'), ensure_ascii=False)
        html = html.replace('</head>', shim + '</head>', 1)
        body = html.encode('utf-8')
        self.send_response(200)
        self.send_header('Content-Type', 'text/html; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _proxy(self, target):
        target = target if target.startswith('http') else 'https://' + target
        proxy = urllib.request.ProxyHandler({'http': UPSTREAM_PROXY, 'https': UPSTREAM_PROXY})
        op = urllib.request.build_opener(urllib.request.HTTPSHandler(context=_ctx), proxy)
        try:
            with op.open(target, timeout=40) as r:
                data = r.read()
                self.send_response(r.status)
                self.send_header('Content-Type', r.headers.get('Content-Type', 'application/octet-stream'))
                self.send_header('Content-Length', str(len(data)))
                self.end_headers()
                self.wfile.write(data)
        except Exception as e:
            self.send_response(502); self.send_header('Content-Type', 'text/plain'); self.end_headers()
            self.wfile.write(('proxy error: %s' % e).encode())

    def log_message(self, format, *args):
        pass

socketserver.TCPServer.allow_reuse_address = True
with socketserver.TCPServer(("127.0.0.1", 8899), H) as httpd:
    httpd.serve_forever()
