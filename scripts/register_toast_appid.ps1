$ErrorActionPreference = 'Stop'
$lnkDir = Join-Path $env:APPDATA "Microsoft\Windows\Start Menu\Programs"
$lnk = Join-Path $lnkDir "Codex-WechatBridge.lnk"
$wsh = New-Object -ComObject WScript.Shell
$s = $wsh.CreateShortcut($lnk)
$s.TargetPath = "$env:SystemRoot\System32\WindowsPowerShell\v1.0\powershell.exe"
$s.Arguments = "-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$PSScriptRoot\show_toast.ps1`""
$s.IconLocation = "$env:SystemRoot\System32\shell32.dll,13"
$s.Save()

Add-Type -TypeDefinition @"
using System;
using System.Runtime.InteropServices;
public class AppIdSetter {
    [StructLayout(LayoutKind.Sequential)]
    public struct PROPERTYKEY { public Guid fmtid; public uint pid; }
    [StructLayout(LayoutKind.Explicit)]
    public struct PROPVARIANT {
        [FieldOffset(0)] public ushort vt;
        [FieldOffset(8)] public IntPtr pointer;
    }
    [ComImport, Guid("886D8EEB-8CF2-4446-8D02-CDBA1DBDCF99"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
    interface IPropertyStore {
        int GetCount(out uint c);
        int GetAt(uint i, out PROPERTYKEY key);
        int GetValue(ref PROPERTYKEY key, out PROPVARIANT pv);
        int SetValue(ref PROPERTYKEY key, ref PROPVARIANT pv);
        int Commit();
    }
    [DllImport("shell32.dll", CharSet = CharSet.Unicode)]
    static extern int SHGetPropertyStoreFromParsingName(string path, IntPtr b, uint flags, ref Guid iid, out IPropertyStore store);
    public static void Set(string lnk, string appId) {
        Guid iid = typeof(IPropertyStore).GUID;
        IPropertyStore store;
        int hr = SHGetPropertyStoreFromParsingName(lnk, IntPtr.Zero, 2, ref iid, out store);
        if (hr != 0) throw new COMException("SHGetPropertyStoreFromParsingName failed: " + hr);
        PROPERTYKEY key;
        key.fmtid = new Guid("9F4C2855-9F79-4B39-A8D0-E1D42DE1D5F3");
        key.pid = 5;
        PROPVARIANT pv;
        pv.vt = 31;
        pv.pointer = Marshal.StringToCoTaskMemUni(appId);
        store.SetValue(ref key, ref pv);
        store.Commit();
        Marshal.FreeCoTaskMem(pv.pointer);
    }
}
"@
[AppIdSetter]::Set($lnk, "Codex.WechatBridge")
Write-Output "registered: $lnk"
