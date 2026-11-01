// The program's own words (the tray's menu, the dialogs), in the language of
// this PC: Italian on an Italian Windows, English otherwise. The window's are
// in web/texts.js.
using System.Globalization;

namespace LocalHome.Desktop
{
    static class Texts
    {
        public static readonly bool Italian = CultureInfo.CurrentUICulture.TwoLetterISOLanguageName == "it";
        public static string Language => Italian ? "it" : "en";

        static string T(string it, string en) => Italian ? it : en;

        public static string Show => T("Mostra", "Show");
        public static string Hide => T("Nascondi", "Hide");
        public static string AlwaysOnTop => T("Sempre in primo piano", "Always on top");
        public static string NotifyZones => T("Avvisa quando si accende una zona", "Tell me when a zone turns on");
        public static string StartWithWindows => T("Avvia con Windows", "Start with Windows");
        public static string BoardAddress => T("Indirizzo della scheda…", "Board address…");
        public static string OpenFull => T("Apri LocalHome nel browser", "Open LocalHome in the browser");
        public static string Exit => T("Esci", "Exit");

        public static string AddressTitle => T("Indirizzo della scheda", "Board address");
        public static string AddressHelp => T(
            "L'indirizzo IP e la porta di LocalHome, ad esempio 192.168.1.20:5000.",
            "LocalHome's IP address and port, for example 192.168.1.20:5000.");
        public static string AddressSave => T("Prova e salva", "Test and save");
        public static string Cancel => T("Annulla", "Cancel");
        public static string AddressInvalid => T("Questo non sembra un indirizzo.", "That doesn't look like an address.");
        public static string AddressNoAnswer(string a) => T(
            $"{a} non risponde come LocalHome. Lo salvo comunque?",
            $"{a} doesn't answer as LocalHome. Save it anyway?");

        public static string StillRunning => T(
            "LocalHome resta attivo qui, per gli avvisi. Per chiuderlo: tasto destro sull'icona, Esci.",
            "LocalHome stays on here, for its notices. To close it: right-click the icon, Exit.");

        public static string SetupAsk => T(
            "Installare LocalHome su questo PC?\n\nNon servono permessi di amministratore e si avvierà con Windows (si può togliere dal menu dell'icona).",
            "Install LocalHome on this PC?\n\nNo administrator rights needed, and it will start with Windows (this can be turned off from its icon's menu).");
        public static string SetupFailed(string why) => T(
            "L'installazione non è riuscita: " + why, "The installation failed: " + why);
        public static string NoWebView => T(
            "Manca Microsoft Edge WebView2 Runtime, che LocalHome usa per la sua finestra. Si scarica da Microsoft: https://go.microsoft.com/fwlink/p/?LinkId=2124703",
            "Microsoft Edge WebView2 Runtime is missing, and LocalHome's window needs it. Get it from Microsoft: https://go.microsoft.com/fwlink/p/?LinkId=2124703");
        public static string UninstallAsk => T(
            "Disinstallare LocalHome da questo PC? La scheda e i suoi dati non vengono toccati.",
            "Uninstall LocalHome from this PC? The board and its data are not touched.");
    }
}
