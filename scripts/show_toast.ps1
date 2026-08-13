param(
    [string]$Title = "微信通道",
    [string]$Text = ""
)
$ErrorActionPreference = 'SilentlyContinue'
Add-Type -AssemblyName System.Runtime.WindowsRuntime
[Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom, ContentType = WindowsRuntime] | Out-Null
[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] | Out-Null

$xml = @"
<toast><visual><binding template="ToastGeneric"><text>$Title</text><text>$Text</text></binding></visual></toast>
"@
$doc = [Windows.Data.Xml.Dom.XmlDocument]::new()
$doc.LoadXml($xml)
$notifier = [Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier("Codex.WechatBridge")
$notifier.Show([Windows.UI.Notifications.ToastNotification]::new($doc))
