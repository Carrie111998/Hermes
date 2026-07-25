using System;
using System.Diagnostics;
using System.IO;
using System.Reflection;
using System.Threading;
using Microsoft.Win32;

class Uninstaller
{
    [STAThread]
    static void Main(string[] args)
    {
        string exePath = Assembly.GetExecutingAssembly().Location;
        string exeDir = Path.GetDirectoryName(exePath);

        // Mode 1: launched from install dir (no args) -> copy to temp and relaunch
        if (args.Length == 0)
        {
            string tempExe = Path.Combine(Path.GetTempPath(), "qiji_uninstall.exe");
            try
            {
                File.Copy(exePath, tempExe, true);
            }
            catch (Exception ex)
            {
                Console.WriteLine("复制卸载程序失败: " + ex.Message);
                Console.WriteLine("按任意键关闭...");
                Console.ReadKey();
                return;
            }
            // Relaunch from temp with install dir as argument
            Process.Start(new ProcessStartInfo
            {
                FileName = tempExe,
                Arguments = "\"" + exeDir + "\"",
                UseShellExecute = false
            });
            return;
        }

        // Mode 2: running from temp -> actual uninstall
        string installDir = args[0];
        // Strip trailing backslash if present
        if (installDir.EndsWith("\\"))
            installDir = installDir.Substring(0, installDir.Length - 1);

        Console.WriteLine();
        Console.WriteLine("  ============================================");
        Console.WriteLine("            奇计 - 卸载程序");
        Console.WriteLine("  ============================================");
        Console.WriteLine();
        Console.WriteLine("  正在卸载奇计...");

        // 1. Kill running Qiji processes
        Console.Write("  停止运行中的进程...");
        try
        {
            foreach (var p in Process.GetProcessesByName("Qiji"))
            {
                try { p.Kill(); p.WaitForExit(5000); } catch { }
            }
            Console.WriteLine(" 完成");
        }
        catch { Console.WriteLine(" 跳过"); }

        Thread.Sleep(1500);

        // 2. Delete install directory
        Console.Write("  删除安装文件...");
        try
        {
            if (Directory.Exists(installDir))
                Directory.Delete(installDir, true);
            Console.WriteLine(" 完成");
        }
        catch (Exception ex)
        {
            Console.WriteLine(" 部分失败");
            Console.WriteLine("    " + ex.Message);
        }

        // 3. Delete start menu shortcuts
        Console.Write("  删除开始菜单快捷方式...");
        try
        {
            string startMenu = Path.Combine(
                Environment.GetFolderPath(Environment.SpecialFolder.Programs), "奇计");
            if (Directory.Exists(startMenu))
                Directory.Delete(startMenu, true);
            Console.WriteLine(" 完成");
        }
        catch { Console.WriteLine(" 跳过"); }

        // 4. Delete desktop shortcut
        Console.Write("  删除桌面快捷方式...");
        try
        {
            string desktopLnk = Path.Combine(
                Environment.GetFolderPath(Environment.SpecialFolder.Desktop), "奇计.lnk");
            if (File.Exists(desktopLnk))
                File.Delete(desktopLnk);
            Console.WriteLine(" 完成");
        }
        catch { Console.WriteLine(" 跳过"); }

        // 5. Delete registry uninstall entry
        Console.Write("  清理注册表...");
        try
        {
            Registry.CurrentUser.DeleteSubKeyTree(
                @"Software\Microsoft\Windows\CurrentVersion\Uninstall\Qiji", false);
            Console.WriteLine(" 完成");
        }
        catch { Console.WriteLine(" 跳过"); }

        // 6. Clean up QIJI_HOME data directory (%LOCALAPPDATA%\qiji)
        Console.Write("  清理数据目录...");
        try
        {
            string qijiHome = Path.Combine(
                Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData), "qiji");
            if (Directory.Exists(qijiHome))
                Directory.Delete(qijiHome, true);
            Console.WriteLine(" 完成");
        }
        catch (Exception ex)
        {
            Console.WriteLine(" 部分失败");
            Console.WriteLine("    " + ex.Message);
        }

        // 7. Remove QIJI_HOME env var from registry
        Console.Write("  清理环境变量...");
        try
        {
            Registry.CurrentUser.DeleteSubKey(
                @"Environment\QIJI_HOME", false);
            // Also try the typed API (more reliable on some Windows versions)
            Environment.SetEnvironmentVariable("QIJI_HOME", null, EnvironmentVariableTarget.User);
            Console.WriteLine(" 完成");
        }
        catch { Console.WriteLine(" 跳过"); }

        Console.WriteLine();
        Console.WriteLine("  ============================================");
        Console.WriteLine("            卸载完成！");
        Console.WriteLine("  ============================================");
        Console.WriteLine();
        Console.Write("  按任意键关闭...");
        Console.ReadKey();

        // 6. Self-delete temp copy (schedule delayed delete)
        try
        {
            string selfPath = Assembly.GetExecutingAssembly().Location;
            Process.Start(new ProcessStartInfo
            {
                FileName = "cmd.exe",
                Arguments = "/c timeout /t 2 >nul & del \"" + selfPath + "\"",
                CreateNoWindow = true,
                UseShellExecute = false
            });
        }
        catch { }
    }
}
