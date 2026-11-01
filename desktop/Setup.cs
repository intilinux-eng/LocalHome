// Installing and removing: for this user only, no administrator rights -
// the program and its libraries in the user's Programs folder, a Start menu
// entry, one in Windows' list of apps, and starting with Windows. Removing
// takes all of it away, and never touches the board.
using System;
using System.Diagnostics;
using System.IO;
using System.Linq;
using System.Reflection;
using System.Windows.Forms;
using Microsoft.Win32;

namespace LocalHome.Desktop
{
    static class Setup
    {
        const string RunKey = @"Software\Microsoft\Windows\CurrentVersion\Run";
        const string UninstallKey = @"Software\Microsoft\Windows\CurrentVersion\Uninstall\LocalHome";
        const string RunName = "LocalHome";

        static string InstallDir => Path.Combine(
            Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData), "Programs", "LocalHome");
        static string InstalledExe => Path.Combine(InstallDir, "LocalHome.exe");
        static string Shortcut => Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.Programs), "LocalHome.lnk");
        static string Self => Assembly.GetExecutingAssembly().Location;

        public static bool StartsWithWindows
        {
            get
            {
                using (var key = Registry.CurrentUser.OpenSubKey(RunKey))
                {
                    return key?.GetValue(RunName) != null;
                }
            }
        }

        public static void SetStartWithWindows(bool on)
        {
            using (var key = Registry.CurrentUser.CreateSubKey(RunKey))
            {
                if (on) key.SetValue(RunName, $"\"{Application.ExecutablePath}\" --tray");
                else key.DeleteValue(RunName, false);
            }
        }

        public static int Install(bool silent)
        {
            if (!silent && MessageBox.Show(Texts.SetupAsk, "LocalHome", MessageBoxButtons.YesNo, MessageBoxIcon.Question) != DialogResult.Yes)
                return 1;
            try
            {
                var firstTime = !File.Exists(InstalledExe);
                StopRunning();
                Directory.CreateDirectory(InstallDir);
                File.Copy(Self, InstalledExe, true);
                var assembly = Assembly.GetExecutingAssembly();
                foreach (var name in assembly.GetManifestResourceNames().Where(n => n.StartsWith("lib/", StringComparison.Ordinal)))
                {
                    using (var from = assembly.GetManifestResourceStream(name))
                    using (var to = File.Create(Path.Combine(InstallDir, name.Substring(4))))
                    {
                        from.CopyTo(to);
                    }
                }
                MakeShortcut();
                using (var key = Registry.CurrentUser.CreateSubKey(UninstallKey))
                {
                    key.SetValue("DisplayName", "LocalHome");
                    key.SetValue("DisplayIcon", InstalledExe);
                    key.SetValue("DisplayVersion", assembly.GetName().Version.ToString(3));
                    key.SetValue("Publisher", "LocalHome");
                    key.SetValue("InstallLocation", InstallDir);
                    key.SetValue("UninstallString", $"\"{InstalledExe}\" --uninstall");
                    key.SetValue("NoModify", 1, RegistryValueKind.DWord);
                    key.SetValue("NoRepair", 1, RegistryValueKind.DWord);
                }
                if (firstTime)
                {
                    using (var key = Registry.CurrentUser.CreateSubKey(RunKey))
                    {
                        key.SetValue(RunName, $"\"{InstalledExe}\" --tray");
                    }
                }
                if (!silent) Process.Start(new ProcessStartInfo(InstalledExe) { UseShellExecute = true });
                return 0;
            }
            catch (Exception e) when (e is IOException || e is UnauthorizedAccessException)
            {
                if (!silent) MessageBox.Show(Texts.SetupFailed(e.Message), "LocalHome", MessageBoxButtons.OK, MessageBoxIcon.Error);
                else Console.Error.WriteLine(e.Message);
                return 2;
            }
        }

        public static int Uninstall(bool silent)
        {
            if (!silent && MessageBox.Show(Texts.UninstallAsk, "LocalHome", MessageBoxButtons.YesNo, MessageBoxIcon.Question) != DialogResult.Yes)
                return 1;
            StopRunning();
            try { File.Delete(Shortcut); } catch (IOException) { }
            using (var key = Registry.CurrentUser.CreateSubKey(RunKey)) key.DeleteValue(RunName, false);
            Registry.CurrentUser.DeleteSubKeyTree(UninstallKey, false);
            // This program is one of the files to remove: a moment after it ends.
            var folders = $"rd /s /q \"{InstallDir}\" & rd /s /q \"{Settings.DefaultDataDir}\"";
            Process.Start(new ProcessStartInfo("cmd.exe", $"/c ping -n 3 127.0.0.1 >nul & {folders}")
            {
                CreateNoWindow = true,
                UseShellExecute = false,
                WorkingDirectory = Path.GetTempPath(),
            });
            return 0;
        }

        // Only the installed copy: a build running elsewhere is left alone.
        static void StopRunning()
        {
            var me = Process.GetCurrentProcess().Id;
            foreach (var process in Process.GetProcessesByName("LocalHome"))
            {
                try
                {
                    if (process.Id == me) continue;
                    if (!string.Equals(process.MainModule.FileName, InstalledExe, StringComparison.OrdinalIgnoreCase)) continue;
                    process.Kill();
                    process.WaitForExit(5000);
                }
                catch (Exception e) when (e is InvalidOperationException || e is System.ComponentModel.Win32Exception) { }
                finally { process.Dispose(); }
            }
        }

        static void MakeShortcut()
        {
            dynamic shell = Activator.CreateInstance(Type.GetTypeFromProgID("WScript.Shell"));
            dynamic link = shell.CreateShortcut(Shortcut);
            link.TargetPath = InstalledExe;
            link.WorkingDirectory = InstallDir;
            link.Description = "LocalHome";
            link.Save();
        }
    }
}
