// Contract Note Extractor — Windows desktop launcher.
//
// The whole tool (index.html, pdf.js, SheetJS and fonts) is embedded in this
// executable, so it runs fully offline. On start it serves the files on a fixed
// local port (127.0.0.1 only — nothing is reachable from the network) and opens
// them in an Edge/Chrome app window. A fixed port keeps the browser storage
// (client master, users, template) the same on every run.
//
// The page sends a small heartbeat; a few minutes after the last window is
// closed the launcher exits by itself. Starting it again while it is running
// just opens another window on the running instance.
package main

import (
	"embed"
	"fmt"
	"io"
	"io/fs"
	"net"
	"net/http"
	"os"
	"os/exec"
	"path/filepath"
	"runtime"
	"strings"
	"sync"
	"time"
)

//go:embed web
var webFS embed.FS

const (
	appTitle  = "Contract Note Extractor"
	appID     = "contract-note-extractor"
	basePort  = 47821           // fixed so browser storage persists between runs
	idleLimit = 5 * time.Minute // exit this long after the last heartbeat
	byeGrace  = 90 * time.Second
)

var (
	mu        sync.Mutex
	lastBeat  = time.Now()
	noBrowser = os.Getenv("CNE_NO_BROWSER") == "1" // for testing
)

func main() {
	site, err := fs.Sub(webFS, "web")
	if err != nil {
		fatal("The tool files are missing from this build.\n\n" + err.Error())
	}
	for p := basePort; p < basePort+10; p++ {
		url := fmt.Sprintf("http://127.0.0.1:%d/", p)
		if alreadyRunning(url) {
			openApp(url)
			return
		}
		ln, err := net.Listen("tcp", fmt.Sprintf("127.0.0.1:%d", p))
		if err != nil {
			continue // port taken by another program — try the next one
		}
		serve(ln, site, url)
		return
	}
	fatal(fmt.Sprintf("Could not start: local ports %d–%d are all in use.", basePort, basePort+9))
}

// alreadyRunning reports whether this tool is already serving on url.
func alreadyRunning(url string) bool {
	c := http.Client{Timeout: 800 * time.Millisecond}
	r, err := c.Get(url + "__cne/ping")
	if err != nil {
		return false
	}
	defer r.Body.Close()
	b, _ := io.ReadAll(io.LimitReader(r.Body, 64))
	return strings.TrimSpace(string(b)) == appID
}

func serve(ln net.Listener, site fs.FS, url string) {
	files := http.FileServer(http.FS(site))
	mux := http.NewServeMux()
	mux.HandleFunc("/__cne/ping", func(w http.ResponseWriter, _ *http.Request) {
		io.WriteString(w, appID)
	})
	mux.HandleFunc("/__cne/beat", func(w http.ResponseWriter, _ *http.Request) {
		mu.Lock()
		lastBeat = time.Now()
		mu.Unlock()
		w.WriteHeader(http.StatusNoContent)
	})
	mux.HandleFunc("/__cne/bye", func(w http.ResponseWriter, _ *http.Request) {
		// A window closed: exit soon unless another window keeps beating.
		mu.Lock()
		if t := time.Now().Add(byeGrace - idleLimit); t.Before(lastBeat) {
			lastBeat = t
		}
		mu.Unlock()
		w.WriteHeader(http.StatusNoContent)
	})
	mux.HandleFunc("/", func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Cache-Control", "no-cache")
		files.ServeHTTP(w, r)
	})

	go func() {
		if err := http.Serve(ln, mux); err != nil {
			fatal("The local server stopped:\n\n" + err.Error())
		}
	}()
	openApp(url)

	for range time.Tick(10 * time.Second) {
		mu.Lock()
		idle := time.Since(lastBeat)
		mu.Unlock()
		if idle > idleLimit {
			os.Exit(0)
		}
	}
}

// openApp opens url in an Edge or Chrome app window (no tabs or address bar),
// falling back to the default browser.
func openApp(url string) {
	if noBrowser {
		return
	}
	if runtime.GOOS != "windows" {
		exec.Command("xdg-open", url).Start()
		return
	}
	var candidates []string
	for _, env := range []string{"ProgramFiles(x86)", "ProgramFiles", "LocalAppData"} {
		base := os.Getenv(env)
		if base == "" {
			continue
		}
		candidates = append(candidates,
			filepath.Join(base, `Microsoft\Edge\Application\msedge.exe`),
			filepath.Join(base, `Google\Chrome\Application\chrome.exe`))
	}
	for _, exe := range candidates {
		if _, err := os.Stat(exe); err == nil {
			if exec.Command(exe, "--app="+url, "--window-size=1400,900").Start() == nil {
				return
			}
		}
	}
	if err := exec.Command("rundll32", "url.dll,FileProtocolHandler", url).Start(); err != nil {
		fatal("Could not open a browser. Open this address manually:\n\n" + url)
	}
}

func fatal(msg string) {
	showError(appTitle, msg)
	os.Exit(1)
}
