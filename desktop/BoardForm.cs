// Which board this window is for: what it's called here and where it is. Asked
// on the first start, changed from the window's gear or the tray's menu. The
// address is tried before it's saved, through the same read the window does.
using System;
using System.Drawing;
using System.Net.Http;
using System.Threading.Tasks;
using System.Windows.Forms;

namespace LocalHome.Desktop
{
    public class BoardForm : Form
    {
        const int MaxName = 40;
        const int FieldWidth = 380;

        readonly TextBox nameBox = new TextBox();
        readonly TextBox addressBox = new TextBox();
        readonly Label note = new Label();
        readonly Button save = new Button();

        public string BoardName { get; private set; }
        public string Address { get; private set; }

        public BoardForm(string currentName, string currentAddress)
        {
            Text = Texts.BoardTitle;
            FormBorderStyle = FormBorderStyle.FixedDialog;
            MaximizeBox = MinimizeBox = false;
            StartPosition = FormStartPosition.CenterScreen;
            AutoScaleMode = AutoScaleMode.Dpi;
            Font = new Font("Segoe UI", 9.5f);
            AutoSize = true;
            AutoSizeMode = AutoSizeMode.GrowAndShrink;
            Padding = new Padding(14);
            try { Icon = Icon.ExtractAssociatedIcon(Application.ExecutablePath); } catch (ArgumentException) { }

            var layout = new TableLayoutPanel { ColumnCount = 1, AutoSize = true, Dock = DockStyle.Fill };
            layout.Controls.Add(Caption(Texts.BoardName));
            nameBox.Width = FieldWidth;
            nameBox.MaxLength = MaxName;
            nameBox.Text = currentName ?? "";
            nameBox.AccessibleName = Texts.BoardName;
            nameBox.Margin = new Padding(0, 0, 0, 10);
            layout.Controls.Add(nameBox);
            layout.Controls.Add(Caption(Texts.BoardAddress));
            addressBox.Width = FieldWidth;
            addressBox.Text = (currentAddress ?? "").Replace("http://", "");
            addressBox.AccessibleName = Texts.BoardAddress;
            layout.Controls.Add(addressBox);
            layout.Controls.Add(new Label { Text = Texts.BoardAddressHelp, AutoSize = true, MaximumSize = new Size(FieldWidth, 0), ForeColor = SystemColors.GrayText, Margin = new Padding(0, 4, 0, 0) });
            note.AutoSize = true;
            note.MaximumSize = new Size(FieldWidth, 0);
            note.ForeColor = Color.Firebrick;
            note.Margin = new Padding(0, 6, 0, 6);
            layout.Controls.Add(note);

            var buttons = new FlowLayoutPanel { FlowDirection = FlowDirection.RightToLeft, AutoSize = true, Dock = DockStyle.Fill, Margin = new Padding(0, 6, 0, 0) };
            var cancel = new Button { Text = Texts.Cancel, DialogResult = DialogResult.Cancel, AutoSize = true };
            save.Text = Texts.BoardSave;
            save.AutoSize = true;
            save.Click += async (s, e) => await TryIt();
            buttons.Controls.Add(cancel);
            buttons.Controls.Add(save);
            layout.Controls.Add(buttons);
            Controls.Add(layout);
            AcceptButton = save;
            CancelButton = cancel;
            ActiveControl = string.IsNullOrEmpty(currentAddress) ? addressBox : nameBox;
        }

        static Label Caption(string text) => new Label { Text = text, AutoSize = true, Margin = new Padding(0, 0, 0, 3) };

        async Task TryIt()
        {
            var address = Settings.NormalizeAddress(addressBox.Text);
            if (address == null)
            {
                note.Text = Texts.AddressInvalid;
                addressBox.Focus();
                return;
            }
            note.Text = "";
            save.Enabled = false;
            var answers = await Answers(address);
            save.Enabled = true;
            if (!answers && MessageBox.Show(this, Texts.AddressNoAnswer(address), Texts.BoardTitle,
                                            MessageBoxButtons.YesNo, MessageBoxIcon.Warning) != DialogResult.Yes) return;
            BoardName = nameBox.Text.Trim();
            Address = address;
            DialogResult = DialogResult.OK;
        }

        static async Task<bool> Answers(string address)
        {
            try
            {
                using (var http = new HttpClient(new HttpClientHandler { UseProxy = false }) { Timeout = TimeSpan.FromSeconds(6) })
                {
                    var text = await http.GetStringAsync(address + "/api/server-time");
                    return text.Contains("epoch_ms");
                }
            }
            catch (Exception e) when (e is HttpRequestException || e is TaskCanceledException)
            {
                return false;
            }
        }
    }
}
