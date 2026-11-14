// What this PC remembers: the board's name and address, the window's choices - in
// the user's own folder, so nothing about this app lives on the board.
using System;
using System.IO;
using System.Web.Script.Serialization;

namespace LocalHome.Desktop
{
    public class Settings
    {
        public const int DefaultPort = 5000;

        public string BoardName { get; set; }
        public string Address { get; set; }
        public bool TopMost { get; set; }
        public bool NotifyZones { get; set; } = true;
        public bool ToldAboutTray { get; set; }
        // The tray icon's number: "auto" the first zone, "" none, else a zone or outside.
        public string TrayFigure { get; set; } = "auto";
        public int X { get; set; } = int.MinValue;
        public int Y { get; set; } = int.MinValue;
        public int Width { get; set; }
        public int Height { get; set; }

        string file;

        // What the window and its notices call the board.
        public string ShownName() => string.IsNullOrWhiteSpace(BoardName) ? "LocalHome" : BoardName.Trim();

        public static string DefaultDataDir =>
            Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData), "LocalHome Desktop");

        public static Settings Load(string dataDir)
        {
            var file = Path.Combine(dataDir, "settings.json");
            Settings loaded = null;
            try
            {
                if (File.Exists(file)) loaded = new JavaScriptSerializer().Deserialize<Settings>(File.ReadAllText(file));
            }
            catch (Exception) { /* a damaged file: as on a first start */ }
            loaded = loaded ?? new Settings();
            loaded.file = file;
            return loaded;
        }

        public void Save()
        {
            try
            {
                File.WriteAllText(file, new JavaScriptSerializer().Serialize(this));
            }
            catch (IOException) { /* tried again at the next change */ }
        }

        // "192.168.1.20:5000", "myboard.local", "http://192.168.1.20:5000/": the
        // base address, or null when it can't be one. The port when none is
        // given is the board's usual one.
        public static string NormalizeAddress(string text)
        {
            text = (text ?? "").Trim().TrimEnd('/');
            if (text.Length == 0) return null;
            var hadScheme = text.Contains("://");
            if (!hadScheme) text = "http://" + text;
            if (!Uri.TryCreate(text, UriKind.Absolute, out var uri)) return null;
            if (uri.Scheme != "http" && uri.Scheme != "https") return null;
            if (string.IsNullOrEmpty(uri.Host)) return null;
            var named = text.Substring(text.IndexOf("://", StringComparison.Ordinal) + 3).Contains(":");
            var port = named ? uri.Port : hadScheme && uri.Scheme == "https" ? 443 : DefaultPort;
            return uri.Scheme + "://" + uri.Host + ":" + port;
        }
    }
}
