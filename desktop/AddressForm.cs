// Where the board is: asked on the first start, changed from the tray's menu.
// Tried before it's saved, through the same read the window does.
using System;
using System.Drawing;
using System.Net.Http;
using System.Threading.Tasks;
using System.Windows.Forms;

namespace LocalHome.Desktop
{
    public class AddressForm : Form
    {
        readonly TextBox box = new TextBox();
        readonly Label note = new Label();
        readonly Button save = new Button();

        public string Address { get; private set; }

        public AddressForm(string current)
        {
            Text = Texts.AddressTitle;
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
            layout.Controls.Add(new Label { Text = Texts.AddressHelp, AutoSize = true, MaximumSize = new Size(380, 0), Margin = new Padding(0, 0, 0, 8) });
            box.Width = 380;
            box.Text = (current ?? "").Replace("http://", "");
            layout.Controls.Add(box);
            note.AutoSize = true;
            note.MaximumSize = new Size(380, 0);
            note.ForeColor = Color.Firebrick;
            note.Margin = new Padding(0, 6, 0, 6);
            layout.Controls.Add(note);

            var buttons = new FlowLayoutPanel { FlowDirection = FlowDirection.RightToLeft, AutoSize = true, Dock = DockStyle.Fill, Margin = new Padding(0, 6, 0, 0) };
            var cancel = new Button { Text = Texts.Cancel, DialogResult = DialogResult.Cancel, AutoSize = true };
            save.Text = Texts.AddressSave;
            save.AutoSize = true;
            save.Click += async (s, e) => await TryIt();
            buttons.Controls.Add(cancel);
            buttons.Controls.Add(save);
            layout.Controls.Add(buttons);
            Controls.Add(layout);
            AcceptButton = save;
            CancelButton = cancel;
        }

        async Task TryIt()
        {
            var address = Settings.NormalizeAddress(box.Text);
            if (address == null)
            {
                note.Text = Texts.AddressInvalid;
                return;
            }
            note.Text = "";
            save.Enabled = false;
            var answers = await Answers(address);
            save.Enabled = true;
            if (!answers && MessageBox.Show(this, Texts.AddressNoAnswer(address), Texts.AddressTitle,
                                            MessageBoxButtons.YesNo, MessageBoxIcon.Warning) != DialogResult.Yes) return;
            Address = address;
            DialogResult = DialogResult.OK;
        }

        static async Task<bool> Answers(string address)
        {
            try
            {
                using (var http = new HttpClient { Timeout = TimeSpan.FromSeconds(6) })
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
