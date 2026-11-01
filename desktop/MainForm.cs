// The window and the tray icon. The page inside is the program's own
// (web/), at an address that exists only here; what it asks under /api/ is
// passed on to the board by this code - that's why the board needs no change,
// not even to be called from another origin. Only the requests the page really
// makes go through: this window can't reach anything else on the board.
using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.Drawing;
using System.IO;
using System.Net.Http;
using System.Reflection;
using System.Runtime.InteropServices;
using System.Text;
using System.Text.RegularExpressions;
using System.Threading.Tasks;
using System.Web.Script.Serialization;
using System.Windows.Forms;
using Microsoft.Web.WebView2.Core;
using Microsoft.Web.WebView2.WinForms;

namespace LocalHome.Desktop
{
    public class MainForm : Form
    {
        const string Origin = "https://localhome.example/";

        static readonly Regex Reads = new Regex(
            @"^/api/(climate/zones|problems|devices|readings|vmc-schedule|covers|server-time)$");
        static readonly Regex Commands = new Regex(
            @"^/api/(vmc-schedule/boost(/cancel)?|covers/[^/]+/action/(open|stop|close)|sensors/[^/]+/(set-value|on)|climate/zones/[^/]+/override(/cancel)?|climate/away(/cancel)?|dehumidifier/(on|off))$");

        readonly Settings settings;
        readonly string dataDir;
        readonly WebView2 web = new WebView2();
        readonly NotifyIcon tray = new NotifyIcon();
        readonly Icon baseIcon;
        readonly HttpClient http = new HttpClient(new HttpClientHandler { UseProxy = false }) { Timeout = TimeSpan.FromSeconds(14) };
        readonly Timer watchdog = new Timer { Interval = 60000 };
        readonly bool startHidden;

        ToolStripMenuItem showItem;
        IntPtr iconHandle = IntPtr.Zero;
        DateTime lastStatus = DateTime.UtcNow;
        string level = "";
        bool quitting;
        bool askingBoard;

        public MainForm(Settings settings, string dataDir, bool startHidden)
        {
            this.settings = settings;
            this.dataDir = dataDir;
            this.startHidden = startHidden;

            Text = settings.ShownName();
            AutoScaleMode = AutoScaleMode.Dpi;
            baseIcon = Icon.ExtractAssociatedIcon(Application.ExecutablePath);
            Icon = baseIcon;
            TopMost = settings.TopMost;
            MinimumSize = new Size(Scaled(320), Scaled(300));
            PlaceWindow();

            web.Dock = DockStyle.Fill;
            Controls.Add(web);

            BuildTray();
            SetLevel("off", settings.ShownName());
            Log("start, board " + settings.Address);

            watchdog.Tick += (s, e) => Watch();
            watchdog.Start();
            Shown += async (s, e) => await StartPage();
            if (startHidden)
            {
                // The window exists, for its page and its notices, without being seen.
                ShowInTaskbar = false;
                WindowState = FormWindowState.Minimized;
            }
        }

        int Scaled(int value) => (int)Math.Round(value * DeviceDpi / 96.0);

        void PlaceWindow()
        {
            StartPosition = FormStartPosition.Manual;
            var area = Screen.PrimaryScreen.WorkingArea;
            var size = settings.Width > 0 ? new Size(settings.Width, settings.Height) : new Size(Scaled(390), Math.Min(Scaled(790), area.Height - Scaled(20)));
            var at = new Point(area.Right - size.Width - Scaled(12), area.Bottom - size.Height - Scaled(12));
            if (settings.X != int.MinValue)
            {
                var saved = new Rectangle(settings.X, settings.Y, size.Width, size.Height);
                // Only where a screen still is: a monitor unplugged doesn't lose the window.
                foreach (var screen in Screen.AllScreens)
                {
                    if (screen.WorkingArea.IntersectsWith(saved)) { at = saved.Location; break; }
                }
            }
            Bounds = new Rectangle(at, size);
        }

        // --------------------------------------------------------- the page --

        async Task StartPage()
        {
            if (startHidden) HideWindow();
            try
            {
                // Hidden, it still has to read the board on time for its notices.
                var options = new CoreWebView2EnvironmentOptions(
                    "--disable-background-timer-throttling --disable-renderer-backgrounding --disable-backgrounding-occluded-windows");
                var environment = await CoreWebView2Environment.CreateAsync(null, Path.Combine(dataDir, "WebView2"), options);
                await web.EnsureCoreWebView2Async(environment);
            }
            catch (Exception e) when (e is WebView2RuntimeNotFoundException || e is COMException)
            {
                Log("no WebView2: " + e.Message);
                MessageBox.Show(Texts.NoWebView, "LocalHome", MessageBoxButtons.OK, MessageBoxIcon.Error);
                Quit();
                return;
            }
            var core = web.CoreWebView2;
            core.Settings.AreDefaultContextMenusEnabled = false;
            core.Settings.IsStatusBarEnabled = false;
            core.Settings.IsZoomControlEnabled = false;
            core.Settings.AreDevToolsEnabled = Debugger.IsAttached;
            core.AddWebResourceRequestedFilter(Origin + "*", CoreWebView2WebResourceContext.All);
            core.WebResourceRequested += OnRequest;
            core.WebMessageReceived += OnMessage;
            core.NavigationStarting += (s, e) => { if (!e.Uri.StartsWith(Origin, StringComparison.Ordinal)) e.Cancel = true; };
            core.NewWindowRequested += (s, e) => e.Handled = true;
            core.ProcessFailed += (s, e) => { Log("page process failed: " + e.ProcessFailedKind); LoadPage(); };
            LoadPage();
        }

        void LoadPage()
        {
            lastStatus = DateTime.UtcNow;
            web.CoreWebView2?.Navigate($"{Origin}index.html?lang={Texts.Language}&board={Uri.EscapeDataString(settings.Address)}&name={Uri.EscapeDataString(settings.ShownName())}");
        }

        async void OnRequest(object sender, CoreWebView2WebResourceRequestedEventArgs e)
        {
            var path = new Uri(e.Request.Uri).AbsolutePath;
            if (!path.StartsWith("/api/", StringComparison.Ordinal))
            {
                e.Response = Local(path);
                return;
            }
            var query = new Uri(e.Request.Uri).Query;
            var method = e.Request.Method;
            byte[] body = null;
            if (e.Request.Content != null)
            {
                using (var buffer = new MemoryStream())
                {
                    e.Request.Content.CopyTo(buffer);
                    body = buffer.ToArray();
                }
            }
            var deferral = e.GetDeferral();
            try
            {
                e.Response = await Forward(method, path, query, body);
            }
            finally
            {
                deferral.Complete();
            }
        }

        CoreWebView2WebResourceResponse Local(string path)
        {
            var name = "web" + (path == "/" ? "/index.html" : path);
            var stream = Assembly.GetExecutingAssembly().GetManifestResourceStream(name);
            if (stream == null) return Answer(404, "Not Found", "text/plain", new byte[0]);
            var type = name.EndsWith(".js") ? "text/javascript; charset=utf-8" : name.EndsWith(".css") ? "text/css; charset=utf-8"
                : name.EndsWith(".html") ? "text/html; charset=utf-8" : "application/octet-stream";
            return web.CoreWebView2.Environment.CreateWebResourceResponse(stream, 200, "OK",
                $"Content-Type: {type}\nCache-Control: no-store");
        }

        async Task<CoreWebView2WebResourceResponse> Forward(string method, string path, string query, byte[] body)
        {
            var allowed = method == "GET" ? Reads.IsMatch(path) : method == "POST" && Commands.IsMatch(path);
            if (!allowed)
            {
                Log("refused: " + method + " " + path);
                return Answer(403, "Forbidden", "application/json", Encoding.UTF8.GetBytes("{\"ok\":false}"));
            }
            try
            {
                using (var request = new HttpRequestMessage(new HttpMethod(method), settings.Address + path + query))
                {
                    if (body != null)
                    {
                        request.Content = new ByteArrayContent(body);
                        request.Content.Headers.TryAddWithoutValidation("Content-Type", "application/json");
                    }
                    using (var response = await http.SendAsync(request))
                    {
                        if (method == "POST") Log($"command: {path} -> {(int)response.StatusCode}");
                        var data = await response.Content.ReadAsByteArrayAsync();
                        return Answer((int)response.StatusCode, "Board", "application/json", data);
                    }
                }
            }
            catch (Exception e) when (e is HttpRequestException || e is TaskCanceledException)
            {
                return Answer(502, "Unreachable", "application/json", Encoding.UTF8.GetBytes("{\"ok\":false,\"unreachable\":true}"));
            }
        }

        CoreWebView2WebResourceResponse Answer(int status, string reason, string type, byte[] data) =>
            web.CoreWebView2.Environment.CreateWebResourceResponse(new MemoryStream(data), status, reason,
                $"Content-Type: {type}\nCache-Control: no-store");

        void OnMessage(object sender, CoreWebView2WebMessageReceivedEventArgs e)
        {
            Dictionary<string, object> message;
            try
            {
                message = new JavaScriptSerializer().Deserialize<Dictionary<string, object>>(e.WebMessageAsJson);
            }
            catch (ArgumentException)
            {
                return;
            }
            string Field(string name) => message.TryGetValue(name, out var value) ? value as string ?? "" : "";
            switch (Field("type"))
            {
                case "status":
                    lastStatus = DateTime.UtcNow;
                    SetLevel(Field("level"), Field("tip"));
                    break;
                case "notify":
                    Notify(Field("kind"), Field("title"), Field("text"), Field("level"));
                    break;
                case "open":
                    OpenInBrowser(Field("path"));
                    break;
                case "settings":
                    // After this message: a dialog held open inside it would hold the page.
                    BeginInvoke((Action)ChangeBoard);
                    break;
            }
        }

        void OpenInBrowser(string path)
        {
            if (!path.StartsWith("/", StringComparison.Ordinal)) path = "/";
            try { Process.Start(new ProcessStartInfo(settings.Address + path) { UseShellExecute = true }); }
            catch (Exception e) when (e is System.ComponentModel.Win32Exception || e is InvalidOperationException) { Log("no browser: " + e.Message); }
        }

        // A page that has stopped speaking is started again. It reads the board
        // by itself, so this only fires if it has truly gone.
        void Watch()
        {
            if (web.CoreWebView2 == null || (DateTime.UtcNow - lastStatus).TotalMinutes < 4) return;
            Log("the page went quiet: loading it again");
            LoadPage();
        }

        // ------------------------------------------------------------ notices --

        void Notify(string kind, string title, string text, string tone)
        {
            if (kind == "zone" && !settings.NotifyZones) return;
            if (title.Length == 0) return;
            if (text.Length == 0) text = " ";
            Log($"notice [{kind}]: {title} | {text.Replace("\n", " / ")}");
            var icon = tone == "warn" ? ToolTipIcon.Warning : tone == "off" ? ToolTipIcon.Error : ToolTipIcon.Info;
            tray.ShowBalloonTip(10000, Cut(title, 63), Cut(text, 255), icon);
        }

        static string Cut(string text, int max) => text.Length <= max ? text : text.Substring(0, max - 1) + "…";

        void SetLevel(string newLevel, string tip)
        {
            tray.Text = Cut(string.IsNullOrEmpty(tip) ? settings.ShownName() : tip, 63);
            if (newLevel == level) return;
            Log($"state: {newLevel} ({tray.Text})");
            level = newLevel;
            var size = SystemInformation.SmallIconSize;
            using (var bitmap = new Bitmap(size.Width, size.Height))
            using (var g = Graphics.FromImage(bitmap))
            using (var small = new Icon(baseIcon, size))
            {
                g.SmoothingMode = System.Drawing.Drawing2D.SmoothingMode.AntiAlias;
                g.DrawIcon(small, new Rectangle(Point.Empty, size));
                var d = (int)Math.Round(size.Width * 0.56);
                var dot = new Rectangle(size.Width - d, size.Height - d, d - 1, d - 1);
                var color = level == "ok" ? Color.FromArgb(46, 157, 98) : level == "warn" ? Color.FromArgb(226, 144, 20)
                    : level == "busy" ? Color.FromArgb(217, 115, 26) : Color.FromArgb(128, 131, 140);
                using (var brush = new SolidBrush(color)) g.FillEllipse(brush, dot);
                using (var pen = new Pen(Color.White, Math.Max(1f, size.Width / 16f))) g.DrawEllipse(pen, dot);
                var handle = bitmap.GetHicon();
                tray.Icon = Icon.FromHandle(handle);
                if (iconHandle != IntPtr.Zero) DestroyIcon(iconHandle);
                iconHandle = handle;
            }
        }

        [DllImport("user32.dll")]
        static extern bool DestroyIcon(IntPtr handle);

        // --------------------------------------------------------------- tray --

        void BuildTray()
        {
            var menu = new ContextMenuStrip();
            showItem = new ToolStripMenuItem(Texts.Show, null, (s, e) => ToggleWindow());
            menu.Items.Add(showItem);
            menu.Items.Add(new ToolStripMenuItem(Texts.OpenFull, null, (s, e) => OpenInBrowser("/")));
            menu.Items.Add(new ToolStripSeparator());
            menu.Items.Add(new ToolStripMenuItem(Texts.AlwaysOnTop, null, (s, e) =>
            {
                settings.TopMost = !settings.TopMost;
                TopMost = settings.TopMost;
                ((ToolStripMenuItem)s).Checked = settings.TopMost;
                settings.Save();
            }) { Checked = settings.TopMost });
            menu.Items.Add(new ToolStripMenuItem(Texts.NotifyZones, null, (s, e) =>
            {
                settings.NotifyZones = !settings.NotifyZones;
                ((ToolStripMenuItem)s).Checked = settings.NotifyZones;
                settings.Save();
            }) { Checked = settings.NotifyZones });
            menu.Items.Add(new ToolStripMenuItem(Texts.StartWithWindows, null, (s, e) =>
            {
                var on = !Setup.StartsWithWindows;
                Setup.SetStartWithWindows(on);
                ((ToolStripMenuItem)s).Checked = on;
            }) { Checked = Setup.StartsWithWindows });
            menu.Items.Add(new ToolStripMenuItem(Texts.BoardSettings, null, (s, e) => ChangeBoard()));
            menu.Items.Add(new ToolStripSeparator());
            menu.Items.Add(new ToolStripMenuItem(Texts.Exit, null, (s, e) => Quit()));
            menu.Opening += (s, e) => showItem.Text = Visible ? Texts.Hide : Texts.Show;
            tray.ContextMenuStrip = menu;
            tray.MouseClick += (s, e) => { if (e.Button == MouseButtons.Left) ToggleWindow(); };
            tray.BalloonTipClicked += (s, e) => ShowWindow();
            tray.Visible = true;
        }

        // Asked from the window and from the tray: one at a time. Hidden, the
        // window can't own it: it opens by itself.
        void ChangeBoard()
        {
            if (askingBoard) return;
            askingBoard = true;
            try
            {
                using (var ask = new BoardForm(settings.BoardName, settings.Address))
                {
                    if ((Visible ? ask.ShowDialog(this) : ask.ShowDialog()) != DialogResult.OK) return;
                    settings.BoardName = ask.BoardName;
                    settings.Address = ask.Address;
                    settings.Save();
                    Text = settings.ShownName();
                    Log($"board now {settings.ShownName()}, {settings.Address}");
                    LoadPage();
                }
            }
            finally
            {
                askingBoard = false;
            }
        }

        void ToggleWindow()
        {
            if (Visible && WindowState != FormWindowState.Minimized) HideWindow();
            else ShowWindow();
        }

        public void ShowWindow()
        {
            ShowInTaskbar = true;
            Show();
            if (WindowState == FormWindowState.Minimized) WindowState = FormWindowState.Normal;
            Activate();
        }

        void HideWindow() => Hide();

        void Quit()
        {
            quitting = true;
            Close();
        }

        protected override void OnFormClosing(FormClosingEventArgs e)
        {
            if (!quitting && e.CloseReason == CloseReason.UserClosing)
            {
                // Closed by its X, it stays in the tray: the notices are its point.
                e.Cancel = true;
                HideWindow();
                if (!settings.ToldAboutTray)
                {
                    settings.ToldAboutTray = true;
                    settings.Save();
                    tray.ShowBalloonTip(8000, "LocalHome", Texts.StillRunning, ToolTipIcon.Info);
                }
                return;
            }
            RememberBounds();
            tray.Visible = false;
            base.OnFormClosing(e);
        }

        protected override void OnResizeEnd(EventArgs e)
        {
            base.OnResizeEnd(e);
            RememberBounds();
        }

        void RememberBounds()
        {
            if (WindowState != FormWindowState.Normal || !Visible) return;
            settings.X = Left;
            settings.Y = Top;
            settings.Width = Width;
            settings.Height = Height;
            settings.Save();
        }

        void Log(string text)
        {
            try
            {
                var file = Path.Combine(dataDir, "app.log");
                if (File.Exists(file) && new FileInfo(file).Length > 1024 * 1024) File.Delete(file);
                File.AppendAllText(file, $"{DateTime.Now:yyyy-MM-dd HH:mm:ss} {text}\r\n");
            }
            catch (IOException) { /* a log is never worth an error */ }
        }

        protected override void Dispose(bool disposing)
        {
            if (disposing)
            {
                watchdog.Dispose();
                tray.Dispose();
                http.Dispose();
                web.Dispose();
                if (iconHandle != IntPtr.Zero) DestroyIcon(iconHandle);
            }
            base.Dispose(disposing);
        }
    }
}
