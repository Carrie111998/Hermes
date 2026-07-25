using System;
using System.Diagnostics;
using System.IO;
using System.Reflection;
using System.Runtime.InteropServices;
using System.Windows.Forms;
using Microsoft.Win32;

class Launcher
{
    [STAThread]
    static void Main()
    {
        string exePath = Assembly.GetExecutingAssembly().Location;

        // ---- Extract embedded 7zr.exe to temp ----
        string tempDir = Path.Combine(Path.GetTempPath(), "QijiSetup_" + Guid.NewGuid().ToString("N").Substring(0, 8));
        Directory.CreateDirectory(tempDir);
        string sevenZipPath = Path.Combine(tempDir, "7zr.exe");

        var asm = Assembly.GetExecutingAssembly();
        string resName = null;
        foreach (var name in asm.GetManifestResourceNames())
        {
            if (name.EndsWith("7zr.exe")) { resName = name; break; }
        }
        if (resName == null)
        {
            MessageBox.Show("Internal error: 7zr.exe resource not found.", "Qiji Setup", MessageBoxButtons.OK, MessageBoxIcon.Error);
            return;
        }
        using (var stream = asm.GetManifestResourceStream(resName))
        using (var outFs = File.Create(sevenZipPath))
        {
            stream.CopyTo(outFs);
        }

        // ---- Show folder browser ----
        Application.EnableVisualStyles();
        string defaultPath = Path.Combine(
            Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData),
            "Programs", "Qiji");

        var dialog = new FolderBrowserDialog();
        dialog.Description = "选择奇计安装目录";
        dialog.SelectedPath = defaultPath;

        if (dialog.ShowDialog() != DialogResult.OK)
        {
            try { Directory.Delete(tempDir, true); } catch { }
            return;
        }

        string installDir = dialog.SelectedPath;
        Directory.CreateDirectory(installDir);

        // ---- Add Windows Defender exclusion for install dir ----
        // -NonInteractive + RedirectStandardInput prevents Add-MpPreference
        // from blocking on an interactive confirmation prompt (which would
        // hang the installer until the user presses a key in the console).
        Console.WriteLine("配置 Windows Defender 排除项（跳过实时扫描）...");
        try
        {
            var dpsi = new ProcessStartInfo
            {
                FileName = "powershell.exe",
                Arguments = "-NoProfile -NonInteractive -Command \"Add-MpPreference -ExclusionPath '\" + installDir + \"' -ErrorAction SilentlyContinue\"",
                UseShellExecute = false,
                CreateNoWindow = true,
                RedirectStandardInput = true
            };
            var p = Process.Start(dpsi);
            p.StandardInput.Close();
            p.WaitForExit(10000);
        }
        catch { /* non-fatal */ }

        // ---- Also exclude the temp dir where 7zr sits ----
        try
        {
            var dpsi2 = new ProcessStartInfo
            {
                FileName = "powershell.exe",
                Arguments = "-NoProfile -NonInteractive -Command \"Add-MpPreference -ExclusionPath '\" + tempDir + \"' -ErrorAction SilentlyContinue\"",
                UseShellExecute = false,
                CreateNoWindow = true,
                RedirectStandardInput = true
            };
            var p2 = Process.Start(dpsi2);
            p2.StandardInput.Close();
            p2.WaitForExit(10000);
        }
        catch { }

        // ---- Extract payload directly from self ----
        // 7z natively finds a .7z archive embedded in the exe by scanning for signature
        Console.WriteLine();
        Console.WriteLine("正在解压文件到: " + installDir);
        Console.WriteLine();

        var psi = new ProcessStartInfo
        {
            FileName = sevenZipPath,
            Arguments = "x \"" + exePath + "\" -o\"" + installDir + "\" -y -mmt=on",
            UseShellExecute = false,
            CreateNoWindow = false
        };
        var process = Process.Start(psi);
        process.WaitForExit();

        if (process.ExitCode != 0)
        {
            MessageBox.Show("解压失败 (代码 " + process.ExitCode + ")", "Qiji Setup", MessageBoxButtons.OK, MessageBoxIcon.Error);
            try { Directory.Delete(tempDir, true); } catch { }
            return;
        }

        // ---- Cleanup temp ----
        try { Directory.Delete(tempDir, true); } catch { }

        // ---- Find app exe ----
        string appExe = Path.Combine(installDir, "Qiji.exe");
        if (!File.Exists(appExe))
        {
            var found = Directory.GetFiles(installDir, "Qiji.exe", SearchOption.AllDirectories);
            if (found.Length > 0) appExe = found[0];
            else
            {
                MessageBox.Show("错误: 解压后未找到 Qiji.exe", "Qiji Setup");
                return;
            }
        }
        string appDir = Path.GetDirectoryName(appExe);

        // ---- Extract uninstall.exe (embedded resource) ----
        foreach (var name in asm.GetManifestResourceNames())
        {
            if (name.EndsWith("uninstall.exe"))
            {
                using (var stream = asm.GetManifestResourceStream(name))
                using (var outFs = File.Create(Path.Combine(appDir, "uninstall.exe")))
                {
                    stream.CopyTo(outFs);
                }
                Console.WriteLine("卸载程序已安装");
                break;
            }
        }

        // ---- Create desktop shortcut ----
        Console.WriteLine("创建桌面快捷方式...");
        CreateShortcut(
            Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.Desktop), "奇计.lnk"),
            appExe, appDir);

        // ---- Create start menu ----
        Console.WriteLine("创建开始菜单...");
        string startMenu = Path.Combine(
            Environment.GetFolderPath(Environment.SpecialFolder.Programs), "奇计");
        Directory.CreateDirectory(startMenu);
        CreateShortcut(
            Path.Combine(startMenu, "奇计.lnk"),
            appExe, appDir);

        // ---- Register uninstall ----
        Console.WriteLine("注册卸载信息...");
        using (var key = Registry.CurrentUser.CreateSubKey(
            @"Software\Microsoft\Windows\CurrentVersion\Uninstall\Qiji"))
        {
            key.SetValue("DisplayName", "奇计");
            key.SetValue("DisplayIcon", appExe + ", 0");
            key.SetValue("UninstallString", Path.Combine(appDir, "uninstall.exe"));
            key.SetValue("InstallLocation", appDir);
            key.SetValue("DisplayVersion", "0.17.0");
            key.SetValue("Publisher", "奇计");
            key.SetValue("NoModify", 1, RegistryValueKind.DWord);
            key.SetValue("NoRepair", 1, RegistryValueKind.DWord);
        }

        // ---- Done ----
        Console.WriteLine();
        Console.WriteLine("============================================");
        Console.WriteLine("          安装完成！");
        Console.WriteLine("============================================");
        Console.WriteLine();
        Console.WriteLine("安装路径:  " + appDir);
        Console.WriteLine("桌面快捷方式: 奇计");
        Console.WriteLine("开始菜单: 奇计");
        Console.WriteLine("卸载: 控制面板 或 运行 uninstall.exe");
        Console.WriteLine();
        Console.WriteLine("首次启动需要初始化后端环境，请耐心等待 1-2 分钟。");
        Console.WriteLine();
        Console.WriteLine("正在启动奇计...");
        // UseShellExecute=true launches Qiji.exe as a detached process via the
        // Windows shell. This prevents the installer's console window from
        // staying open as a parent of the Qiji process.
        var launchPsi = new ProcessStartInfo
        {
            FileName = appExe,
            WorkingDirectory = appDir,
            UseShellExecute = true
        };
        Process.Start(launchPsi);

        // Brief pause so the user sees "启动成功" before the window closes.
        System.Threading.Thread.Sleep(2000);
    }

    static void CreateShortcut(string shortcutPath, string targetPath, string workingDir)
    {
        Type t = Type.GetTypeFromProgID("WScript.Shell");
        object shell = Activator.CreateInstance(t);
        object shortcut = t.InvokeMember("CreateShortcut",
            BindingFlags.InvokeMethod, null, shell, new object[] { shortcutPath });
        Type st = shortcut.GetType();
        st.InvokeMember("TargetPath", BindingFlags.SetProperty, null, shortcut, new object[] { targetPath });
        st.InvokeMember("WorkingDirectory", BindingFlags.SetProperty, null, shortcut, new object[] { workingDir });
        st.InvokeMember("IconLocation", BindingFlags.SetProperty, null, shortcut, new object[] { targetPath + ", 0" });
        st.InvokeMember("Description", BindingFlags.SetProperty, null, shortcut, new object[] { "奇计" });
        st.InvokeMember("Save", BindingFlags.InvokeMethod, null, shortcut, null);
    }
}
