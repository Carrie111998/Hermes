using System;
using System.Diagnostics;
using System.IO;
using System.Reflection;
using System.Runtime.InteropServices;
using System.Windows.Forms;
using Microsoft.Win32;

class Launcher
{
    [DllImport("kernel32.dll", SetLastError = true)]
    static extern bool AllocConsole();

    [DllImport("kernel32.dll", SetLastError = true)]
    static extern bool FreeConsole();

    // --- Native methods for WM_SETTINGCHANGE broadcast ---
    static class NativeMethods
    {
        public static readonly IntPtr HWND_BROADCAST = new IntPtr(0xffff);
        public const int WM_SETTINGCHANGE = 0x001A;
        public const int SMTO_ABORTIFHUNG = 0x0002;

        [DllImport("user32.dll", SetLastError = true, CharSet = CharSet.Auto)]
        public static extern IntPtr SendMessageTimeout(
            IntPtr hWnd, uint Msg, IntPtr wParam, string lParam,
            uint fuFlags, uint uTimeout, out IntPtr lpdwResult);
    }

    [STAThread]
    static void Main()
    {
        // Allocate a console for install progress output.
        // The exe is compiled as /target:winexe (GUI subsystem) so it has no
        // console by default. We create one here, then free it before launching
        // Qiji so the console window closes cleanly.
        AllocConsole();

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
        Console.WriteLine("配置 Windows Defender 排除项（跳过实时扫描）...");
        try
        {
            var dpsi = new ProcessStartInfo
            {
                FileName = "powershell.exe",
                Arguments = "-NoProfile -NonInteractive -Command \"Add-MpPreference -ExclusionPath '" + installDir + "' -ErrorAction SilentlyContinue\"",
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
                Arguments = "-NoProfile -NonInteractive -Command \"Add-MpPreference -ExclusionPath '" + tempDir + "' -ErrorAction SilentlyContinue\"",
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

        // ---- Initialize Qiji data directory ----
        Console.WriteLine("初始化数据目录...");
        string qijiHome = Path.Combine(
            Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData), "qiji");
        Directory.CreateDirectory(qijiHome);

        // Set QIJI_HOME (User scope) so Qiji finds its data dir
        Registry.SetValue(@"HKEY_CURRENT_USER\Environment", "QIJI_HOME", qijiHome);

        // Remove stale HERMES_HOME (User scope) — leftover from old Hermes installs
        // that pointed to %LOCALAPPDATA%\hermes, causing config confusion
        try
        {
            using (var envKey = Registry.CurrentUser.OpenSubKey(@"Environment", true))
            {
                if (envKey != null && envKey.GetValue("HERMES_HOME") != null)
                {
                    envKey.DeleteValue("HERMES_HOME", false);
                    Console.WriteLine("已清除旧的 HERMES_HOME 环境变量");
                }
            }
        }
        catch { /* non-fatal */ }

        // Broadcast WM_SETTINGCHANGE so other processes pick up the env change
        NativeMethods.SendMessageTimeout(
            NativeMethods.HWND_BROADCAST, NativeMethods.WM_SETTINGCHANGE,
            IntPtr.Zero, "Environment",
            NativeMethods.SMTO_ABORTIFHUNG, 5000, out _);

        // Create default config.yaml with Chinese locale if missing
        string configPath = Path.Combine(qijiHome, "config.yaml");
        if (!File.Exists(configPath))
        {
            File.WriteAllText(configPath, "display:\n  language: zh\n",
                System.Text.Encoding.UTF8);
            Console.WriteLine("已创建默认配置 (language=zh)");
        }

        // ---- Done ----
        Console.WriteLine();
        Console.WriteLine("============================================");
        Console.WriteLine("          安装完成！");
        Console.WriteLine("============================================");
        Console.WriteLine();
        Console.WriteLine("正在启动奇计...");
        Console.WriteLine();

        // ---- Launch Qiji and close console immediately ----
        // FreeConsole detaches us (and the console window) from any child processes.
        // UseShellExecute=true launches Qiji as a fully independent process.
        // Environment.Exit(0) terminates the launcher immediately so the console
        // window disappears the moment Qiji starts.
        FreeConsole();

        var launchPsi = new ProcessStartInfo
        {
            FileName = appExe,
            WorkingDirectory = appDir,
            UseShellExecute = true
        };
        Process.Start(launchPsi);

        Environment.Exit(0);
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
