// Where the program starts: as the installer when it's alone (the file you
// download), as the window when its libraries are beside it (installed, or a
// build). WebView2's types are only touched in App, after this choice.
using System;
using System.IO;
using System.Reflection;
using System.Threading;
using System.Windows.Forms;

namespace LocalHome.Desktop
{
    static class Program
    {
        [STAThread]
        static int Main(string[] args)
        {
            Application.EnableVisualStyles();
            Application.SetCompatibleTextRenderingDefault(false);

            if (HasFlag(args, "--uninstall")) return Setup.Uninstall(HasFlag(args, "--silent"));
            var here = Path.GetDirectoryName(Assembly.GetExecutingAssembly().Location);
            if (!File.Exists(Path.Combine(here, "Microsoft.Web.WebView2.Core.dll")))
                return Setup.Install(HasFlag(args, "--silent"));
            return App.Run(args);
        }

        public static bool HasFlag(string[] args, string flag) => Array.IndexOf(args, flag) >= 0;

        public static string Option(string[] args, string name)
        {
            var i = Array.IndexOf(args, name);
            return i >= 0 && i + 1 < args.Length ? args[i + 1] : null;
        }
    }

    static class App
    {
        public static int Run(string[] args)
        {
            var dataDir = Program.Option(args, "--data-dir") ?? Settings.DefaultDataDir;
            Directory.CreateDirectory(dataDir);
            // One window per data folder: starting it again shows the one running.
            var key = "LocalHome.Desktop." + dataDir.ToLowerInvariant().GetHashCode().ToString("x");
            using (var mutex = new Mutex(true, key, out var first))
            using (var show = new EventWaitHandle(false, EventResetMode.AutoReset, key + ".show"))
            {
                if (!first)
                {
                    show.Set();
                    return 0;
                }
                var settings = Settings.Load(dataDir);
                var address = Program.Option(args, "--address");
                if (address != null) settings.Address = Settings.NormalizeAddress(address);
                if (string.IsNullOrEmpty(settings.Address))
                {
                    using (var ask = new AddressForm(settings.Address))
                    {
                        if (ask.ShowDialog() != DialogResult.OK) return 0;
                        settings.Address = ask.Address;
                    }
                }
                settings.Save();
                using (var form = new MainForm(settings, dataDir, Program.HasFlag(args, "--tray")))
                {
                    var waiter = new Thread(() =>
                    {
                        while (show.WaitOne())
                        {
                            try { form.BeginInvoke((Action)form.ShowWindow); } catch (InvalidOperationException) { }
                        }
                    }) { IsBackground = true };
                    waiter.Start();
                    Application.Run(form);
                }
            }
            return 0;
        }
    }
}
