# Fixed local update client. Release data is JSON, never executable Tcl.
namespace eval ::JLC2CaptureUpdates {
    variable window .jlc2capture_updates
    variable channel ""
    variable responseFile ""
    variable outputText ""
    variable operation ""
    variable status ""
    variable checked [dict create]
    variable expectedVersion ""
}

proc ::JLC2CaptureUpdates::show {args} {
    variable window
    variable status
    if {[llength [info commands winfo]] && [winfo exists $window]} {
        wm deiconify $window
        raise $window
        return
    }
    if {[catch {
        ::JLC2Capture::requireTk
        if {![dict size $::JLC2Capture::configuration]} { ::JLC2Capture::configure }
        toplevel $window
        wm title $window "JLC/LCSC - \u6269\u5c55\u66f4\u65b0"
        wm geometry $window 520x300
        wm minsize $window 440 260
        wm protocol $window WM_DELETE_WINDOW [list wm withdraw $window]
        ttk::label $window.version -text "\u5f53\u524d\u7248\u672c [::JLC2Capture::version]" -padding 12
        text $window.notes -wrap word -height 7 -state disabled -font TkDefaultFont
        ttk::label $window.status -textvariable ::JLC2CaptureUpdates::status -wraplength 480 -padding 12
        ttk::frame $window.buttons -padding 12
        ttk::button $window.buttons.check -text "\u68c0\u67e5\u66f4\u65b0" -command {::JLC2CaptureUpdates::start check}
        ttk::button $window.buttons.install -text "\u4e0b\u8f7d\u5e76\u5b89\u88c5" -state disabled -command {::JLC2CaptureUpdates::start download}
        pack $window.buttons.check $window.buttons.install -side left -padx 4
        grid $window.version -row 0 -column 0 -sticky w
        grid $window.notes -row 1 -column 0 -sticky nsew -padx 12
        grid $window.status -row 2 -column 0 -sticky ew
        grid $window.buttons -row 3 -column 0 -sticky e
        grid columnconfigure $window 0 -weight 1
        grid rowconfigure $window 1 -weight 1
        after 0 {::JLC2CaptureUpdates::start check}
    } message]} { ::JLC2Capture::showError $message }
}

proc ::JLC2CaptureUpdates::setBusy {value} {
    variable window
    variable checked
    if {![llength [info commands winfo]] || ![winfo exists $window]} { return }
    $window.buttons.check configure -state [expr {$value ? "disabled" : "normal"}]
    set available [expr {[::JLC2Capture::get $checked update_available 0] in {true 1}}]
    $window.buttons.install configure -state [expr {!$value && $available ? "normal" : "disabled"}]
}

proc ::JLC2CaptureUpdates::command {kind output} {
    variable expectedVersion
    if {$kind ni {check download apply}} { error "Unsupported update operation" }
    set config $::JLC2Capture::configuration
    set url [::JLC2Capture::get $config update_manifest_url]
    set keys [::JLC2Capture::get $config update_trusted_keys]
    if {$url eq ""} { error "\u5c1a\u672a\u914d\u7f6e\u5728\u7ebf\u66f4\u65b0\u670d\u52a1\u5668\u3002" }
    if {$keys eq "" || ![file isfile $keys]} { error "\u7f3a\u5c11\u66f4\u65b0\u9a8c\u8bc1\u516c\u94a5\uff0c\u8bf7\u4f7f\u7528\u5b8c\u6574\u5b89\u88c5\u5305\u3002" }
    # Updates also use pythonw on Windows.  The updater writes an atomic JSON
    # response, so there is no reason to create a visible console process.
    set command [list [::JLC2Capture::workerPython] -u -m jlc2cadence.updater $kind \
        --manifest-url $url --trusted-keys $keys --current-version [::JLC2Capture::version] --output $output]
    set cache [::JLC2Capture::get $config update_cache_dir]
    if {$cache eq ""} {
        if {[info exists ::env(LOCALAPPDATA)] && $::env(LOCALAPPDATA) ne ""} {
            set cache [file join $::env(LOCALAPPDATA) JLC2Cadence updates]
        } else {
            set cache [file join $::JLC2Capture::libraryRoot .updates]
        }
    }
    if {$cache ne ""} { lappend command --cache-dir $cache }
    if {[::JLC2Capture::get $config update_allow_loopback_http 0] in {1 true}} { lappend command --allow-loopback-http }
    if {$kind ne "check"} {
        if {$expectedVersion eq ""} { error "\u8bf7\u5148\u68c0\u67e5\u66f4\u65b0\u7248\u672c\u3002" }
        lappend command --expected-version $expectedVersion
    }
    return $command
}

proc ::JLC2CaptureUpdates::start {kind} {
    variable channel
    variable responseFile
    variable outputText
    variable operation
    variable status
    variable checked
    variable expectedVersion
    if {$channel ne ""} { return }
    if {[catch {
        if {$kind eq "check"} {
            set checked [dict create]
            set expectedVersion ""
        } elseif {$kind eq "download"} {
            if {[::JLC2Capture::get $checked update_available 0] ni {1 true}} { error "\u8bf7\u5148\u68c0\u67e5\u66f4\u65b0\u7248\u672c\u3002" }
            set expectedVersion [::JLC2Capture::get $checked latest_version]
        }
        if {$kind eq "apply" && ($::JLC2Capture::channel ne "" || $::JLC2CaptureDock::channel ne "")} {
            error "\u8bf7\u7b49\u5f85\u5143\u5668\u4ef6\u5bfc\u5165\u5b8c\u6210\u540e\u518d\u5b89\u88c5\u66f4\u65b0\u3002"
        }
        set directory [file join $::JLC2Capture::libraryRoot .capture_jobs]
        file mkdir $directory
        set responseFile [file join $directory "update_[pid]_[clock microseconds].json"]
        set worker [command $kind $responseFile]
        set outputText ""
        set operation $kind
        set saved [dict create]
        foreach key {PYTHONPATH PYTHONIOENCODING} {
            if {[info exists ::env($key)]} { dict set saved $key $::env($key) }
        }
        set ::env(PYTHONPATH) [file join $::JLC2Capture::projectRoot src]
        set ::env(PYTHONIOENCODING) utf-8
        set failed [catch {set channel [open [concat [list |] $worker [list 2>@1]] r]} message options]
        foreach key {PYTHONPATH PYTHONIOENCODING} {
            if {[dict exists $saved $key]} { set ::env($key) [dict get $saved $key] } else { unset ::env($key) }
        }
        if {$failed} { return -options $options $message }
        fconfigure $channel -blocking 0 -encoding utf-8 -translation auto
        fileevent $channel readable ::JLC2CaptureUpdates::receive
        set status [dict get [dict create check "\u6b63\u5728\u68c0\u67e5\u66f4\u65b0..." \
            download "\u6b63\u5728\u4e0b\u8f7d\u5e76\u9a8c\u8bc1\u5b89\u88c5\u5305..." apply "\u6b63\u5728\u542f\u52a8\u66f4\u65b0\u5b89\u88c5\u7a0b\u5e8f..."] $kind]
        setBusy 1
    } message]} { set status $message; setBusy 0 }
}

proc ::JLC2CaptureUpdates::receive {} {
    variable channel
    variable responseFile
    variable outputText
    variable operation
    variable status
    variable checked
    variable expectedVersion
    variable window
    if {$channel eq ""} { return }
    if {[catch {read $channel} received]} {
        catch {fileevent $channel readable {}}
        catch {close $channel}
        set channel ""
        set status $received
        setBusy 0
        return
    }
    append outputText $received
    if {[string length $outputText] > 65536} { set outputText [string range $outputText end-65535 end] }
    if {![eof $channel]} { return }
    fileevent $channel readable {}
    set failed [catch {close $channel} closeMessage]
    set channel ""
    set applyNext 0
    if {[catch {
        set data [::JLC2Capture::readJson $responseFile]
        if {![::JLC2Capture::get $data ok 0]} { error [::JLC2Capture::backendError $data] }
        if {$failed} { error "$closeMessage\n$outputText" }
        if {$operation eq "check"} {
            set checked $data
            set status [::JLC2Capture::get $data user_message]
            if {[::JLC2Capture::get $data update_available 0] in {1 true}} {
                set status "\u53d1\u73b0\u65b0\u7248\u672c [::JLC2Capture::get $data latest_version]\uff0c\u53ef\u4e0b\u8f7d\u5b89\u88c5\u3002"
            } elseif {$status eq ""} { set status "\u5f53\u524d\u5df2\u662f\u6700\u65b0\u7248\u672c\u3002" }
            if {[llength [info commands winfo]] && [winfo exists $window.notes]} {
                $window.notes configure -state normal
                $window.notes delete 1.0 end
                $window.notes insert end [::JLC2Capture::get $data notes]
                $window.notes configure -state disabled
            }
        } elseif {$operation eq "download"} {
            if {[::JLC2Capture::get $data status] ne "downloaded"} { error [::JLC2Capture::backendError $data] }
            if {[::JLC2Capture::get $data version] ne $expectedVersion} { error "\u670d\u52a1\u5668\u7248\u672c\u5df2\u53d8\u5316\uff0c\u8bf7\u91cd\u65b0\u68c0\u67e5\u66f4\u65b0\u3002" }
            set applyNext 1
        } elseif {$operation eq "apply"} {
            if {[::JLC2Capture::get $data status] ne "started"} { error [::JLC2Capture::backendError $data] }
            set checked [dict create]
            set status "\u66f4\u65b0\u5b89\u88c5\u7a0b\u5e8f\u5df2\u542f\u52a8\u3002\u5b8c\u6210\u5b89\u88c5\u540e\uff0c\u8bf7\u91cd\u65b0\u542f\u52a8 Capture \u4f7f\u65b0\u7248\u672c\u751f\u6548\u3002"
        }
    } message]} { set status $message; set checked [dict create] }
    if {$applyNext} { start apply; return }
    setBusy 0
}
