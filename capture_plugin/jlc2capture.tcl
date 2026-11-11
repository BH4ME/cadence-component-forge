# Native OrCAD Capture component search and placement (17.2 / 17.4).
namespace eval ::JLC2Capture {
    variable pluginDir [file dirname [file normalize [info script]]]
    variable projectRoot [file dirname $pluginDir]
    variable window .jlc2capture
    variable query ""
    variable libraryRoot [file join $projectRoot jlc_library]
    variable python "python"
    variable cadenceRoot ""
    variable viewerBaseUrl ""
    variable configuration [dict create]
    variable hostVersion ""
    variable status ""
    variable results {}
    variable imported [dict create]
    variable selected ""
    variable busy 0
    variable channel ""
    variable responseFile ""
    variable outputText ""
    variable operation ""
    variable operationPart ""
    variable operationRoot ""
    variable placeRequested 0
    variable registered 0
    variable previewData [dict create]
    variable previewPart ""
    variable previewRoot ""
    variable previewPolicy ""
    variable stepFiles {}
    variable stepViewerUrl ""
    variable manufacturerFilter "\u5168\u90e8"
    variable packageFilter "\u5168\u90e8"
    variable stockOnly 0
    variable page 1
    variable pageSize 30
    variable total ""
    variable hasMore 0
    variable pageLabel "1"
    variable resultCount ""
    variable searchQuery ""
    variable pathChannel ""
    variable pathResponse ""
    variable pathOperationKey {}
    variable pathRegisteredKey {}
    variable pathWarning ""
    variable capturePathWarning ""
}

proc ::JLC2Capture::get {data key {fallback ""}} {
    if {[dict exists $data $key]} { return [dict get $data $key] }
    return $fallback
}

proc ::JLC2Capture::workerPython {} {
    # Resolve bare interpreter names as well as configured absolute paths.
    # Falling back to python.exe when pythonw is missing opens a console in
    # Capture; report that configuration error instead of silently doing so.
    variable python
    if {$::tcl_platform(platform) ne "windows"} { return [string cat $python] }
    set executable [string cat $python]
    if {[file pathtype $executable] ne "absolute"} {
        set resolved [auto_execok $executable]
        if {[llength $resolved] == 1} { set executable [lindex $resolved 0] }
    }
    set tail [string tolower [file tail $executable]]
    if {$tail ni {python python.exe python3 python3.exe}} { return [string cat $executable] }
    set candidate [file join [file dirname $executable] pythonw.exe]
    if {[file isfile $candidate]} { return [string cat $candidate] }
    error "Python's windowless runtime (pythonw.exe) is missing. Repair the plugin installation or configure a Python installation with pythonw.exe."
}

# Use Capture's own directory-registration API so the live footprint viewer
# and Capture.ini agree. It appends to the user's existing directory list.
proc ::JLC2Capture::registerFootprintPath {{root ""} {footprint ""}} {
    variable libraryRoot
    if {$root eq ""} { set root $libraryRoot }
    set directory [file normalize [file join $root allegro symbols]]
    if {![file isdirectory $directory]} { return 0 }
    if {$footprint ne ""} {
        if {[file tail $footprint] ne $footprint} { error "Invalid footprint name" }
        foreach extension {dra psm} {
            set path [file join $directory "[string tolower $footprint].$extension"]
            if {![file isfile $path] || [file size $path] == 0} {
                error "Generated footprint file is missing: $path"
            }
        }
    } elseif {![llength [glob -nocomplain -directory $directory *.dra]]} {
        return 0
    }
    if {![llength [info commands capSetFootPrintPathToCaptureINI]]} { return 0 }
    capSetFootPrintPathToCaptureINI $directory
    return 1
}

proc ::JLC2Capture::requireTk {} {
    # Hide only the implicit root created by this plugin's first Tk load.
    # Existing Tk roots owned by another Capture extension are left alone.
    set hadTk [expr {[llength [info commands winfo]] > 0}]
    package require Tk 8.5
    if {!$hadTk} { wm withdraw . }
}

proc ::JLC2Capture::pathRegistrationWarning {message} {
    variable pathWarning
    variable status
    set pathWarning $message
    set status $message
    puts "JLC2Capture library paths: $message"
    if {[llength [info commands ::JLC2CaptureDock::emit]] &&
        [info exists ::JLC2CaptureDock::widget] && $::JLC2CaptureDock::widget != 0} {
        package require json::write
        ::JLC2CaptureDock::emit paths [::json::write object ok false message [::json::write string $message]]
    }
}

proc ::JLC2Capture::registerLibraryPaths {} {
    variable libraryRoot
    variable cadenceRoot
    variable projectRoot
    variable pathChannel
    variable pathResponse
    variable pathOperationKey
    variable pathRegisteredKey
    if {$pathChannel ne ""} { return }
    set key [list [file normalize $libraryRoot] $cadenceRoot]
    if {$key eq $pathRegisteredKey} { return }
    if {![llength [glob -nocomplain -directory [file join $libraryRoot allegro symbols] *.dra]]} { return }
    if {[catch {
        set directory [file join $libraryRoot .capture_jobs]
        file mkdir $directory
        set pathResponse [file join $directory "paths_[pid]_[clock microseconds].json"]
        set command [list [workerPython] -u -m jlc2cadence.capture_bridge register-paths --library-root $libraryRoot --output $pathResponse]
        if {$cadenceRoot ne ""} { lappend command --cadence-root $cadenceRoot }
        set saved [dict create]
        foreach name {PYTHONPATH PYTHONIOENCODING} {
            if {[info exists ::env($name)]} { dict set saved $name $::env($name) }
        }
        set ::env(PYTHONPATH) [file join $projectRoot src]
        if {[dict exists $saved PYTHONPATH] && [dict get $saved PYTHONPATH] ne ""} {
            append ::env(PYTHONPATH) [expr {$::tcl_platform(platform) eq "windows" ? ";" : ":"}] [dict get $saved PYTHONPATH]
        }
        set ::env(PYTHONIOENCODING) utf-8
        set failed [catch {set pathChannel [open [concat [list |] $command [list 2>@1]] r]} message options]
        foreach name {PYTHONPATH PYTHONIOENCODING} {
            if {[dict exists $saved $name]} { set ::env($name) [dict get $saved $name] } else { unset ::env($name) }
        }
        if {$failed} { return -options $options $message }
        set pathOperationKey $key
        fconfigure $pathChannel -blocking 0 -encoding utf-8 -translation auto
        fileevent $pathChannel readable ::JLC2Capture::receiveLibraryPaths
    } message]} { pathRegistrationWarning $message }
}

proc ::JLC2Capture::configureLibraryPaths {} {
    variable capturePathWarning
    set capturePathWarning ""
    if {[catch {registerFootprintPath} message]} {
        set capturePathWarning $message
        pathRegistrationWarning $message
    }
    registerLibraryPaths
}

proc ::JLC2Capture::receiveLibraryPaths {} {
    variable pathChannel
    variable pathResponse
    variable pathOperationKey
    variable pathRegisteredKey
    variable pathWarning
    variable capturePathWarning
    variable libraryRoot
    variable cadenceRoot
    if {$pathChannel eq ""} { return }
    set readFailed [catch {read $pathChannel 65536} text]
    if {!$readFailed && ![eof $pathChannel]} { return }
    catch {fileevent $pathChannel readable {}}
    set failed [catch {close $pathChannel} closeMessage]
    set pathChannel ""
    if {[catch {
        if {$readFailed} { error $text }
        set data [readJson $pathResponse]
        if {![get $data ok 0]} { error [backendError $data] }
        if {$failed} { error $closeMessage }
        set registration [get $data path_registration]
        set state [get $registration status]
        if {$state eq "registered"} {
            set pathRegisteredKey $pathOperationKey
            set pathWarning $capturePathWarning
        } elseif {$state ne "missing_assets"} {
            error "Unable to register the Allegro library paths: $state"
        }
    } message]} { pathRegistrationWarning $message }
    if {$pathOperationKey ne [list [file normalize $libraryRoot] $cadenceRoot]} {
        after 0 ::JLC2Capture::registerLibraryPaths
    }
}

proc ::JLC2Capture::validatePublishedAssets {data} {
    foreach path [get $data published_files {}] {
        if {![file isfile $path] || [file size $path] == 0} {
            error "Generated library file is missing or empty: $path"
        }
    }
}

proc ::JLC2Capture::backendError {data} {
    foreach key {user_message error message} {
        set message [get $data $key]
        if {[string trim $message] ne ""} { return $message }
    }
    return "Backend failed"
}

proc ::JLC2Capture::sourceExactResult {data} {
    if {[catch {dict get $data conversion_policy} policy]} { return 0 }
    return [expr {$policy eq "source_exact"}]
}

proc ::JLC2Capture::cachedImport {key} {
    variable imported
    if {![dict exists $imported $key]} { return [dict create] }
    set data [dict get $imported $key]
    if {![sourceExactResult $data] || [get $data build_revision] ne "7"} {
        dict unset imported $key
        showPreviews [dict create]
        return [dict create]
    }
    return $data
}

proc ::JLC2Capture::readJson {path} {
    package require json
    set fh [open $path r]
    fconfigure $fh -encoding utf-8
    set failed [catch {
        set text [read $fh 16777217]
    } message options]
    close $fh
    if {$failed} { return -options $options $message }
    if {[string length $text] > 16777216} { error "JSON response exceeds 16 MB" }
    return [::json::json2dict $text]
}

proc ::JLC2Capture::encodeBase64 {text} {
    set bytes [encoding convertto utf-8 $text]
    if {![catch {binary encode base64 -maxlen 0 $bytes} encoded]} { return $encoded }
    package require base64
    return [::base64::encode -maxlen 0 $bytes]
}

proc ::JLC2Capture::decodeBase64 {text} {
    if {[catch {binary decode base64 $text} bytes]} {
        package require base64
        set bytes [::base64::decode $text]
    }
    return [encoding convertfrom utf-8 $bytes]
}

proc ::JLC2Capture::version {} {
    variable pluginDir
    return [get [readJson [file join $pluginDir version.json]] version]
}

proc ::JLC2Capture::configure {} {
    variable pluginDir
    variable python
    variable libraryRoot
    variable cadenceRoot
    variable viewerBaseUrl
    variable imported
    variable configuration
    variable hostVersion
    set previousViewer $viewerBaseUrl
    set previousRoot $libraryRoot
    set path [file join $pluginDir config.json]
    if {[info exists ::env(JLC2CAPTURE_CONFIG)] && $::env(JLC2CAPTURE_CONFIG) ne ""} {
        set path $::env(JLC2CAPTURE_CONFIG)
        if {![file isfile $path]} { error "The installed JLC/LCSC configuration is missing: $path" }
    }
    set config [dict create]
    if {[file exists $path]} {
        set config [readJson $path]
        set python [get $config python $python]
        set libraryRoot [get $config library_root $libraryRoot]
        set cadenceRoot [get $config cadence_root $cadenceRoot]
        set viewerBaseUrl [get $config viewer_base_url $viewerBaseUrl]
    }
    # Keep a running Capture session on the runtime selected by its bootstrap.
    # An installer may update the shared configuration before this host restarts.
    if {[info exists ::env(JLC2CAPTURE_PYTHON)] && $::env(JLC2CAPTURE_PYTHON) ne ""} {
        set python $::env(JLC2CAPTURE_PYTHON)
    }
    # Work on a string copy so Tcl's file commands do not change the internal
    # object type of the configured value exposed to legacy Capture scripts.
    set pythonValue [string range $python 0 end]
    if {$::tcl_platform(platform) eq "windows" && [string tolower [file tail $pythonValue]] eq "python.exe"} {
        set worker [file join [file dirname $pythonValue] pythonw.exe]
        if {[file isfile $worker]} { set python $worker }
    }
    if {[info exists ::env(JLC2CAPTURE_TRUSTED_KEYS)] && $::env(JLC2CAPTURE_TRUSTED_KEYS) ne ""} {
        dict set config update_trusted_keys $::env(JLC2CAPTURE_TRUSTED_KEYS)
    }
    set configuration $config
    if {[info exists ::env(JLC2CAPTURE_CADENCE_ROOT)] && $::env(JLC2CAPTURE_CADENCE_ROOT) ne ""} {
        set cadenceRoot [file normalize $::env(JLC2CAPTURE_CADENCE_ROOT)]
    }
    # The running executable is authoritative when both Cadence versions are installed.
    set executable [info nameofexecutable]
    if {[string equal -nocase [file tail $executable] Capture.exe]} {
        set cadenceRoot [file dirname [file dirname [file dirname $executable]]]
    }
    set hostVersion ""
    if {[info exists ::env(JLC2CAPTURE_CADENCE_VERSION)]} { set hostVersion $::env(JLC2CAPTURE_CADENCE_VERSION) }
    set roots [get $config cadence_roots [dict create]]
    dict for {candidate root} $roots {
        if {$cadenceRoot ne "" && [string equal -nocase [file normalize $root] [file normalize $cadenceRoot]]} { set hostVersion $candidate }
    }
    if {$hostVersion eq ""} { regexp {17\.[24]} $cadenceRoot hostVersion }
    if {[get $config isolate_host_libraries 0] in {1 true}} {
        if {$hostVersion ni {17.2 17.4}} { error "Cannot identify the active Cadence 17.2/17.4 library target" }
        set libraryRoot [file join [get $config library_root $libraryRoot] cadence-$hostVersion]
    }
    if {$viewerBaseUrl ne $previousViewer || $libraryRoot ne $previousRoot} {
        set imported [dict create]
        showPreviews [dict create]
    }
    configureLibraryPaths
}

proc ::JLC2Capture::enabled {args} { return 1 }

# Capture menu Update actions require the literal true/false result; their
# handler is distinct from RegisterAction's numeric shouldProcess predicate.
proc ::JLC2Capture::menuEnabled {args} { return true }

proc ::JLC2Capture::register {} {
    variable registered
    if {$registered || ![llength [info commands RegisterAction]]} { return }
    RegisterAction JLC2CaptureSearch ::JLC2Capture::enabled "" ::JLC2Capture::openBrowser ""
    RegisterAction JLC2CaptureUpdate ::JLC2Capture::enabled "" ::JLC2Capture::menuEnabled ""
    RegisterAction JLC2CaptureExtensionUpdate ::JLC2Capture::enabled "" ::JLC2CaptureUpdates::show ""
    InsertXMLMenu [list [list JLC2Capture] "0" Help [list popup "JLC/LCSC" "0" "" "" "" ""] ""]
    InsertXMLMenu [list [list JLC2Capture Search] "" "" [list action "Search Components..." "0" JLC2CaptureSearch JLC2CaptureUpdate "" "" "" "JLC/LCSC"] ""]
    InsertXMLMenu [list [list JLC2Capture ExtensionUpdate] "" "" [list action "Check for Updates..." "0" JLC2CaptureExtensionUpdate JLC2CaptureUpdate "" "" "" "JLC/LCSC"] ""]
    set registered 1
    catch {RefreshMenu}
}

proc ::JLC2Capture::show {args} {
    variable window
    variable query
    variable status
    if {[catch {requireTk} message]} {
        puts "JLC2Capture: $message"
        if {[llength [info commands COrSmiUtils_sShowMessageBox]]} {
            COrSmiUtils_sShowMessageBox $message JLC2Capture 16
        }
        return
    }
    if {[catch {configure} message]} { showError $message; return }
    if {[winfo exists $window]} {
        wm deiconify $window
        raise $window
        focus $window.search.query
        setBusy $::JLC2Capture::busy
        return
    }
    toplevel $window
    wm title $window "JLC/LCSC - \u5143\u5668\u4ef6"
    wm geometry $window 1000x740
    wm minsize $window 800 600
    wm protocol $window WM_DELETE_WINDOW [list wm withdraw $window]
    grid columnconfigure $window 0 -weight 1
    grid rowconfigure $window 2 -weight 1

    ttk::frame $window.search -padding {12 12 12 8}
    ttk::label $window.search.label -text "\u6599\u53f7 / \u578b\u53f7"
    ttk::entry $window.search.query -textvariable ::JLC2Capture::query -width 48
    ttk::button $window.search.go -text "\u641c\u7d22" -command ::JLC2Capture::search
    grid $window.search.label -row 0 -column 0 -padx {0 8}
    grid $window.search.query -row 0 -column 1 -sticky ew
    grid $window.search.go -row 0 -column 2 -padx {8 0}
    grid columnconfigure $window.search 1 -weight 1
    grid $window.search -row 0 -column 0 -sticky ew
    bind $window.search.query <Return> ::JLC2Capture::search

    ttk::frame $window.location -padding {12 0 12 10}
    ttk::label $window.location.label -text "\u5e93\u6587\u4ef6\u5939"
    ttk::entry $window.location.path -textvariable ::JLC2Capture::libraryRoot
    ttk::button $window.location.choose -text "..." -width 3 -command ::JLC2Capture::chooseRoot
    grid $window.location.label -row 0 -column 0 -padx {0 8}
    grid $window.location.path -row 0 -column 1 -sticky ew
    grid $window.location.choose -row 0 -column 2 -padx {8 0}
    grid columnconfigure $window.location 1 -weight 1
    grid $window.location -row 1 -column 0 -sticky ew

    ttk::panedwindow $window.body -orient vertical
    ttk::frame $window.body.list
    ttk::frame $window.body.list.filters -padding {0 0 0 6}
    ttk::label $window.body.list.filters.scope -text "\u5f53\u524d\u9875"
    ttk::label $window.body.list.filters.manufacturerLabel -text "\u5382\u5546"
    ttk::combobox $window.body.list.filters.manufacturer -textvariable ::JLC2Capture::manufacturerFilter -state readonly -width 21
    ttk::label $window.body.list.filters.packageLabel -text "\u5c01\u88c5"
    ttk::combobox $window.body.list.filters.package -textvariable ::JLC2Capture::packageFilter -state readonly -width 15
    ttk::checkbutton $window.body.list.filters.stock -text "\u6709\u5e93\u5b58" -variable ::JLC2Capture::stockOnly -command ::JLC2Capture::applyFilters
    foreach widget {scope manufacturerLabel manufacturer packageLabel package stock} {
        pack $window.body.list.filters.$widget -side left -padx {0 8}
    }
    foreach widget {manufacturer package} {
        bind $window.body.list.filters.$widget <<ComboboxSelected>> ::JLC2Capture::applyFilters
    }
    grid $window.body.list.filters -row 0 -column 0 -columnspan 2 -sticky ew
    ttk::treeview $window.body.list.table -columns {id title manufacturer package stock step} -show headings -selectmode browse
    foreach {id label width} {id {LCSC} 95 title {\u578b\u53f7} 245 manufacturer {\u5382\u5546} 140 package {\u5c01\u88c5} 125 stock {\u5e93\u5b58} 80 step {STEP} 65} {
        $window.body.list.table heading $id -text [subst -nocommands -novariables $label]
        $window.body.list.table column $id -width $width -minwidth 55
    }
    ttk::scrollbar $window.body.list.ys -orient vertical -command [list $window.body.list.table yview]
    ttk::scrollbar $window.body.list.xs -orient horizontal -command [list $window.body.list.table xview]
    $window.body.list.table configure -yscrollcommand [list $window.body.list.ys set] -xscrollcommand [list $window.body.list.xs set]
    grid $window.body.list.table -row 1 -column 0 -sticky nsew
    grid $window.body.list.ys -row 1 -column 1 -sticky ns
    grid $window.body.list.xs -row 2 -column 0 -sticky ew
    ttk::frame $window.body.list.paging -padding {0 6 0 6}
    ttk::label $window.body.list.paging.count -textvariable ::JLC2Capture::resultCount
    ttk::button $window.body.list.paging.previous -text "\u25c0" -width 3 -command {::JLC2Capture::searchPage [expr {$::JLC2Capture::page - 1}]}
    ttk::label $window.body.list.paging.page -textvariable ::JLC2Capture::pageLabel -width 12 -anchor center
    ttk::button $window.body.list.paging.next -text "\u25b6" -width 3 -command {::JLC2Capture::searchPage [expr {$::JLC2Capture::page + 1}]}
    pack $window.body.list.paging.count -side left
    pack $window.body.list.paging.next $window.body.list.paging.page $window.body.list.paging.previous -side right
    grid $window.body.list.paging -row 3 -column 0 -columnspan 2 -sticky ew
    grid columnconfigure $window.body.list 0 -weight 1
    grid rowconfigure $window.body.list 1 -weight 1
    ttk::notebook $window.body.details
    foreach {kind label} {preview {\u56fe\u7b26 / \u5c01\u88c5} summary {\u8be6\u60c5}} {
        ttk::frame $window.body.details.$kind
        $window.body.details add $window.body.details.$kind -text [subst -nocommands -novariables $label]
    }
    foreach {kind label column} {symbol {\u56fe\u7b26} 0 footprint {\u5c01\u88c5} 1} {
        ttk::frame $window.body.details.preview.$kind
        ttk::label $window.body.details.preview.$kind.label -text [subst -nocommands -novariables $label] -padding {8 4}
        canvas $window.body.details.preview.$kind.canvas -background #f7f8fa -highlightthickness 0 -height 240 -width 300
        pack $window.body.details.preview.$kind.label -fill x
        pack $window.body.details.preview.$kind.canvas -fill both -expand 1
        grid $window.body.details.preview.$kind -row 0 -column $column -sticky nsew -padx 1
        grid columnconfigure $window.body.details.preview $column -weight 1 -uniform previews
        bind $window.body.details.preview.$kind.canvas <Configure> [list ::JLC2Capture::drawPreview $kind]
    }
    grid rowconfigure $window.body.details.preview 0 -weight 1
    text $window.body.details.summary.text -height 7 -wrap word -state disabled -relief flat -padx 8 -pady 8
    ttk::scrollbar $window.body.details.summary.ys -command [list $window.body.details.summary.text yview]
    $window.body.details.summary.text configure -yscrollcommand [list $window.body.details.summary.ys set]
    pack $window.body.details.summary.ys -side right -fill y
    pack $window.body.details.summary.text -fill both -expand 1
    $window.body add $window.body.list -weight 3
    $window.body add $window.body.details -weight 2
    grid $window.body -row 2 -column 0 -sticky nsew -padx 12
    bind $window.body.list.table <<TreeviewSelect>> ::JLC2Capture::select
    bind $window.body.list.table <Double-1> [list ::JLC2Capture::importPart 1]

    ttk::frame $window.actions -padding 12
    ttk::button $window.actions.import -text "\u5bfc\u5165\u5e93" -command [list ::JLC2Capture::importPart 0] -state disabled
    ttk::button $window.actions.place -text "\u5bfc\u5165\u5e76\u653e\u7f6e" -command [list ::JLC2Capture::importPart 1] -state disabled
    ttk::button $window.actions.step -text "3D STEP" -command ::JLC2Capture::viewStep -state disabled
    ttk::button $window.actions.folder -text "\u6253\u5f00\u6587\u4ef6\u5939" -command ::JLC2Capture::openRoot
    ttk::progressbar $window.actions.progress -mode indeterminate -length 100
    pack $window.actions.import $window.actions.place $window.actions.step -side left -padx {0 8}
    pack $window.actions.folder -side right
    pack $window.actions.progress -side right -padx 12
    grid $window.actions -row 3 -column 0 -sticky ew
    ttk::label $window.status -textvariable ::JLC2Capture::status -padding {12 0 12 10} -wraplength 880
    grid $window.status -row 4 -column 0 -sticky ew
    set status ""
    drawPreview symbol
    drawPreview footprint
    updateFilters
    updatePaging
    focus $window.search.query
}

proc ::JLC2Capture::chooseRoot {} {
    variable window
    variable libraryRoot
    set path [tk_chooseDirectory -parent $window -initialdir $libraryRoot -mustexist 0]
    if {$path ne ""} {
        set libraryRoot $path
        configureLibraryPaths
    }
}

proc ::JLC2Capture::openRoot {} {
    variable libraryRoot
    if {![file isdirectory $libraryRoot]} { return }
    if {[catch {exec explorer.exe [file nativename $libraryRoot] &} message]} { showError $message }
}

proc ::JLC2Capture::setBusy {value} {
    variable busy
    variable window
    variable selected
    set busy $value
    if {![llength [info commands winfo]] || ![winfo exists $window]} { return }
    set state [expr {$busy ? "disabled" : "normal"}]
    foreach path {search.go location.path location.choose} { $window.$path configure -state $state }
    foreach path {manufacturer package} {
        $window.body.list.filters.$path configure -state [expr {$busy ? "disabled" : "readonly"}]
    }
    $window.body.list.filters.stock configure -state $state
    set actionState [expr {$busy || $selected eq "" ? "disabled" : "normal"}]
    $window.actions.import configure -state $actionState
    $window.actions.place configure -state $actionState
    variable stepFiles
    variable stepViewerUrl
    $window.actions.step configure -state [expr {$busy || (![llength $stepFiles] && $stepViewerUrl eq "") ? "disabled" : "normal"}]
    if {$busy} { $window.actions.progress start 12 } else { $window.actions.progress stop }
    updatePaging
}

proc ::JLC2Capture::search {} {
    variable busy
    variable query
    variable selected
    variable status
    variable searchQuery
    variable page
    variable hasMore
    variable total
    if {$busy || [string trim $query] eq ""} { return }
    set searchQuery [string trim $query]
    set page 1
    set hasMore 0
    set total ""
    set selected ""
    showPreviews [dict create]
    searchPage 1
}

proc ::JLC2Capture::searchPage {target} {
    variable busy
    variable searchQuery
    variable selected
    variable status
    variable page
    variable pageSize
    variable hasMore
    if {$busy || $searchQuery eq "" || ![string is integer -strict $target] || $target < 1} { return }
    if {$target > $page && !$hasMore} { return }
    set selected ""
    showPreviews [dict create]
    set status "\u6b63\u5728\u641c\u7d22..."
    start search [list --query $searchQuery --page $target --limit $pageSize]
}

proc ::JLC2Capture::start {kind arguments} {
    variable python
    variable projectRoot
    variable libraryRoot
    variable cadenceRoot
    variable viewerBaseUrl
    variable channel
    variable responseFile
    variable outputText
    variable operation
    set operation $kind
    set outputText ""
    if {[catch {
        set jobDir [file join $libraryRoot .capture_jobs]
        file mkdir $jobDir
        set responseFile [file join $jobDir "[pid]_[clock microseconds].json"]
        set command [list [workerPython] -u -m jlc2cadence.capture_bridge $kind {*}$arguments --library-root $libraryRoot --output $responseFile]
        if {$kind eq "import" && $cadenceRoot ne ""} { lappend command --cadence-root $cadenceRoot }
        if {$kind eq "import" && $viewerBaseUrl ne ""} { lappend command --viewer-base-url $viewerBaseUrl }
        set oldPath [expr {[info exists ::env(PYTHONPATH)] ? $::env(PYTHONPATH) : ""}]
        set oldEncoding [expr {[info exists ::env(PYTHONIOENCODING)] ? $::env(PYTHONIOENCODING) : ""}]
        set pathExisted [info exists ::env(PYTHONPATH)]
        set encodingExisted [info exists ::env(PYTHONIOENCODING)]
        set ::env(PYTHONPATH) [file join $projectRoot src]
        if {$oldPath ne ""} { append ::env(PYTHONPATH) [expr {$::tcl_platform(platform) eq "windows" ? ";" : ":"}] $oldPath }
        set ::env(PYTHONIOENCODING) utf-8
        set failed [catch {
            set channel [open [concat [list |] $command [list 2>@1]] r]
        } message options]
        if {$pathExisted} { set ::env(PYTHONPATH) $oldPath } else { unset ::env(PYTHONPATH) }
        if {$encodingExisted} { set ::env(PYTHONIOENCODING) $oldEncoding } else { unset ::env(PYTHONIOENCODING) }
        if {$failed} { return -options $options $message }
        fconfigure $channel -blocking 0 -encoding utf-8 -translation auto
        fileevent $channel readable ::JLC2Capture::receive
        setBusy 1
    } message]} { setBusy 0; showError $message }
}

proc ::JLC2Capture::receive {} {
    variable channel
    variable outputText
    variable responseFile
    variable operation
    variable status
    if {$channel eq ""} { return }
    if {[catch {read $channel} received]} {
        catch {fileevent $channel readable {}}
        catch {close $channel}
        set channel ""
        setBusy 0
        showError $received
        return
    }
    append outputText $received
    if {[string length $outputText] > 65536} { set outputText [string range $outputText end-65535 end] }
    if {![eof $channel]} { return }
    fileevent $channel readable {}
    set exitError [catch {close $channel} exitMessage]
    set channel ""
    setBusy 0
    if {[catch {
        if {![file exists $responseFile]} {
            error "[string trim $exitMessage]\n[string trim $outputText]"
        }
        set data [readJson $responseFile]
        if {![get $data ok 0]} { error [backendError $data] }
        if {$exitError} { error "$exitMessage\n$outputText" }
        if {$operation eq "search"} { showResults $data } else { finishImport $data }
    } message]} { showError $message }
}

proc ::JLC2Capture::showResults {data} {
    variable results
    variable status
    variable page
    variable pageSize
    variable total
    variable hasMore
    set results [get $data results {}]
    set page [get $data page 1]
    set pageSize [get $data page_size 30]
    set total [get $data total ""]
    set hasMore [expr {[get $data has_more 0] in {1 true}}]
    updateFilters
    applyFilters
    updatePaging
    set status "[llength $results] \u4e2a\u7ed3\u679c"
    set warnings [get $data warnings {}]
    if {[llength $warnings]} { append status "  [join $warnings {; }]" }
}

proc ::JLC2Capture::updateFilters {} {
    variable results
    variable window
    variable manufacturerFilter
    variable packageFilter
    foreach {key variableName} {manufacturer manufacturerFilter package packageFilter} {
        set values {}
        foreach row $results {
            set value [get $row $key]
            if {$value ne "" && $value ni $values} { lappend values $value }
        }
        set values [linsert [lsort -dictionary $values] 0 "\u5168\u90e8"]
        if {[set $variableName] ni $values} { set $variableName "\u5168\u90e8" }
        if {[llength [info commands winfo]] && [winfo exists $window.body.list.filters.$key]} {
            $window.body.list.filters.$key configure -values $values
        }
    }
}

proc ::JLC2Capture::matchesFilters {row} {
    variable manufacturerFilter
    variable packageFilter
    variable stockOnly
    if {$manufacturerFilter ne "\u5168\u90e8" && [get $row manufacturer] ne $manufacturerFilter} { return 0 }
    if {$packageFilter ne "\u5168\u90e8" && [get $row package] ne $packageFilter} { return 0 }
    if {$stockOnly} {
        set stock [get $row stock ""]
        if {![numeric $stock] || $stock <= 0} { return 0 }
    }
    return 1
}

proc ::JLC2Capture::applyFilters {} {
    variable results
    variable window
    variable selected
    variable resultCount
    variable busy
    if {![llength [info commands winfo]] || ![winfo exists $window.body.list.table]} { return }
    set table $window.body.list.table
    $table delete [$table children {}]
    set index 0
    set preferred ""
    set shown 0
    foreach row $results {
        set rowIndex $index
        incr index
        if {![matchesFilters $row]} { continue }
        set step [get $row has_step ""]
        set stepLabel "?"
        if {$step eq "true" || $step eq "1"} { set stepLabel "\u53ef\u7528" }
        if {$step eq "false" || $step eq "0"} { set stepLabel "\u65e0" }
        set stock [get $row stock ""]
        if {![numeric $stock]} { set stock "?" }
        $table insert {} end -id $rowIndex -values [list [get $row lcsc_id] [get $row title] [get $row manufacturer] [get $row package] $stock $stepLabel]
        if {[get $row lcsc_id] eq $selected} { set preferred $rowIndex }
        incr shown
    }
    set resultCount "\u5f53\u524d\u9875 $shown / [llength $results]"
    if {$shown} {
        if {$preferred eq ""} { set preferred [lindex [$table children {}] 0] }
        $table selection set $preferred
        $table focus $preferred
        select
    } else {
        set selected ""
        showPreviews [dict create]
        showDetails ""
        setBusy $busy
    }
}

proc ::JLC2Capture::updatePaging {} {
    variable window
    variable page
    variable pageSize
    variable total
    variable hasMore
    variable busy
    variable pageLabel
    set pageLabel $page
    if {[string is integer -strict $total] && $total >= 0 && $pageSize > 0} {
        set pageLabel "$page / [expr {max(1,int(ceil($total/double($pageSize))))}]"
    }
    if {![llength [info commands winfo]] || ![winfo exists $window.body.list.paging]} { return }
    $window.body.list.paging.previous configure -state [expr {$busy || $page <= 1 ? "disabled" : "normal"}]
    $window.body.list.paging.next configure -state [expr {$busy || !$hasMore ? "disabled" : "normal"}]
}

proc ::JLC2Capture::select {} {
    variable window
    variable results
    variable selected
    variable busy
    variable previewPart
    variable previewRoot
    variable imported
    variable libraryRoot
    set selection [$window.body.list.table selection]
    if {![llength $selection]} { set selected ""; setBusy $busy; return }
    set row [lindex $results [lindex $selection 0]]
    set selected [get $row lcsc_id]
    set key [list [file normalize $libraryRoot] $selected]
    set cached [cachedImport $key]
    if {$cached ne ""} {
        showPreviews $cached
    } elseif {$selected ne $previewPart || $previewRoot eq "" || [file normalize $libraryRoot] ne [file normalize $previewRoot]} {
        showPreviews [dict create]
    }
    set details "$selected   [get $row title]\n[get $row manufacturer]   [get $row package]\n\n[get $row description]"
    showDetails $details
    setBusy $busy
}

proc ::JLC2Capture::showDetails {text} {
    variable window
    $window.body.details.summary.text configure -state normal
    $window.body.details.summary.text delete 1.0 end
    $window.body.details.summary.text insert end $text
    $window.body.details.summary.text configure -state disabled
}

proc ::JLC2Capture::numeric {value} {
    if {![string is double -strict $value]} { return 0 }
    if {[catch {expr {abs(double($value)) < 1.0e12}} valid]} { return 0 }
    return $valid
}

proc ::JLC2Capture::padOutline {pad} {
    set size [get $pad size {0 0}]
    if {[llength $size] != 2} { return {} }
    lassign $size width height
    set x [get $pad x 0]; set y [get $pad y 0]; set rotation [get $pad rotation 0]
    foreach value [list $width $height $x $y $rotation] {
        if {![numeric $value]} { return {} }
    }
    if {$width <= 0 || $height <= 0} { return {} }
    set angle [expr {$rotation * acos(-1) / 180.0}]
    set points {}
    if {[get $pad shape rect] in {circle oval}} {
        for {set i 0} {$i < 40} {incr i} {
            set theta [expr {$i * 2.0 * acos(-1) / 40}]
            lappend points [list [expr {$width * cos($theta) / 2}] [expr {$height * sin($theta) / 2}]]
        }
    } else {
        foreach {sx sy} {-1 -1 1 -1 1 1 -1 1} {
            lappend points [list [expr {$sx * $width / 2}] [expr {$sy * $height / 2}]]
        }
    }
    set result {}
    foreach point $points {
        lassign $point dx dy
        lappend result [list [expr {$x + $dx*cos($angle) - $dy*sin($angle)}] [expr {$y + $dx*sin($angle) + $dy*cos($angle)}]]
    }
    return $result
}

proc ::JLC2Capture::pinLine {pin} {
    set x [get $pin x 0]; set y [get $pin y 0]
    if {![numeric $x] || ![numeric $y]} { return {} }
    set endX [get $pin end_x ""]; set endY [get $pin end_y ""]
    if {![numeric $endX] || ![numeric $endY]} {
        set length [get $pin length 1]
        if {![numeric $length] || $length <= 0} { set length 1 }
        set endX $x; set endY $y
        switch -- [get $pin side left] {
            left { set endX [expr {$x + $length}] }
            right { set endX [expr {$x - $length}] }
            top { set endY [expr {$y - $length}] }
            bottom { set endY [expr {$y + $length}] }
        }
    }
    return [list [list $x $y] [list $endX $endY]]
}

proc ::JLC2Capture::fitPreview {points width height {margin 32}} {
    set valid {}
    foreach point $points {
        if {[llength $point] == 2 && [numeric [lindex $point 0]] && [numeric [lindex $point 1]]} { lappend valid $point }
    }
    if {![llength $valid]} { set valid {{-1 -1} {1 1}} }
    lassign [lindex $valid 0] minX minY
    set maxX $minX; set maxY $minY
    foreach point $valid {
        lassign $point x y
        set minX [expr {min($minX,$x)}]; set maxX [expr {max($maxX,$x)}]
        set minY [expr {min($minY,$y)}]; set maxY [expr {max($maxY,$y)}]
    }
    set scale [expr {min(max(1.0,$width-2*$margin)/max(0.01,$maxX-$minX), max(1.0,$height-2*$margin)/max(0.01,$maxY-$minY))}]
    return [list $scale [expr {$width/2.0 - ($minX+$maxX)*$scale/2}] [expr {$height/2.0 + ($minY+$maxY)*$scale/2}]]
}

proc ::JLC2Capture::projectPoints {points transform} {
    lassign $transform scale offsetX offsetY
    set result {}
    foreach point $points {
        lassign $point x y
        if {[numeric $x] && [numeric $y]} { lappend result [expr {$x*$scale+$offsetX}] [expr {-$y*$scale+$offsetY}] }
    }
    return $result
}

proc ::JLC2Capture::showPreviews {data} {
    variable previewData
    variable previewPart
    variable previewRoot
    variable previewPolicy
    variable stepFiles
    variable stepViewerUrl
    set previewData [get $data preview_data [dict create]]
    set previewPart [get $data part_id [get $data lcsc_id]]
    set previewRoot [get $data library_root]
    set previewPolicy [get $data conversion_policy]
    set stepFiles {}
    foreach path [get $data step_files {}] {
        if {[file isfile $path] && [string tolower [file extension $path]] in {.step .stp}} { lappend stepFiles $path }
    }
    set stepViewerUrl [get $data step_viewer_url]
    if {![regexp {^https?://} $stepViewerUrl]} { set stepViewerUrl "" }
    if {[llength [info commands winfo]]} {
        drawPreview symbol
        drawPreview footprint
    }
}

proc ::JLC2Capture::drawPreview {kind} {
    variable window
    variable previewData
    variable previewPolicy
    set canvas $window.body.details.preview.$kind.canvas
    if {![winfo exists $canvas]} { return }
    $canvas delete all
    set width [winfo width $canvas]; set height [winfo height $canvas]
    if {$width < 20 || $height < 20} { return }
    set data [get $previewData $kind [dict create]]
    if {$previewPolicy eq "source_exact" || [get $data conversion_policy] eq "source_exact" || [get [get $data properties [dict create]] Conversion_Policy] eq "source_exact"} {
        $canvas create text [expr {$width/2}] [expr {$height/2}] -width [expr {max(80,$width-48)}] -justify center \
            -text "Tk \u4e0d\u652f\u6301\u539f\u59cb\u51e0\u4f55\u9884\u89c8\u3002\n\u8bf7\u4f7f\u7528 Capture CEF \u9762\u677f\u9884\u89c8\u3002" -fill #67717b -font {SegoeUI 10}
        return
    }
    set graphics [get $data graphics {}]
    set pads [get $data pads {}]
    set pins [get $data pins {}]
    set points {}
    foreach graphic $graphics { lappend points {*}[get $graphic points {}] }
    foreach pad $pads { lappend points {*}[padOutline $pad] }
    foreach pin $pins { lappend points {*}[pinLine $pin] }
    if {![llength $points]} {
        $canvas create text [expr {$width/2}] [expr {$height/2}] -text "\u2014" -fill #67717b -font {SegoeUI 18}
        return
    }
    set transform [fitPreview $points $width $height 40]
    set scale [lindex $transform 0]
    foreach graphic $graphics {
        set projected [projectPoints [get $graphic points {}] $transform]
        set layer [get $graphic layer ""]
        set color [expr {[string match *assembly* $layer] ? "#78838e" : "#263746"}]
        set lineWidth [get $graphic width 0.1]
        if {![numeric $lineWidth]} { set lineWidth 0.1 }
        set lineWidth [expr {max(1,min(3,$lineWidth*$scale))}]
        if {[get $graphic kind] eq "region" && [llength $projected] >= 6} {
            $canvas create polygon $projected -fill #dce1e5 -outline $color -width $lineWidth
        } elseif {[llength $projected] >= 4} {
            $canvas create line $projected -fill $color -width $lineWidth
        } elseif {[get $graphic kind] eq "text"} {
            set position [projectPoints [list [list [get $graphic x 0] [get $graphic y 0]]] $transform]
            if {[llength $position] == 2} { $canvas create text {*}$position -text [get $graphic text] -fill $color -font {SegoeUI 9} }
        }
    }
    foreach pad $pads {
        set outline [projectPoints [padOutline $pad] $transform]
        if {[llength $outline] < 6} { continue }
        $canvas create polygon $outline -fill #ddb34e -outline #8c6b26 -width 1
        set center [projectPoints [list [list [get $pad x 0] [get $pad y 0]]] $transform]
        set hole [get $pad hole_diameter 0]
        if {[numeric $hole] && $hole > 0} {
            lassign $center x y
            set radius [expr {$hole*$scale/2}]
            $canvas create oval [expr {$x-$radius}] [expr {$y-$radius}] [expr {$x+$radius}] [expr {$y+$radius}] -fill #f7f8fa -outline #8c6b26
        }
        $canvas create text {*}$center -text [get $pad number] -fill #2d281a -font {SegoeUI 9}
    }
    foreach pin $pins {
        set line [projectPoints [pinLine $pin] $transform]
        if {[llength $line] != 4} { continue }
        $canvas create line $line -fill #23785e -width 2
        lassign $line x y endX endY
        set anchor [expr {$endX >= $x ? "w" : "e"}]
        set offset [expr {$endX >= $x ? 5 : -5}]
        $canvas create text [expr {$endX+$offset}] $endY -text [get $pin name] -anchor $anchor -fill #263746 -font {SegoeUI 9}
        $canvas create text $x [expr {$y-9}] -text [get $pin number] -fill #23785e -font {SegoeUI 8}
    }
}

proc ::JLC2Capture::viewStep {} {
    variable stepViewerUrl
    variable stepFiles
    if {$stepViewerUrl ne ""} {
        set target $stepViewerUrl
    } elseif {[llength $stepFiles]} {
        set target [file nativename [lindex $stepFiles 0]]
    } else {
        showError "\u6ca1\u6709\u53ef\u7528\u7684 STEP \u6a21\u578b\u3002"
        return
    }
    if {[catch {exec rundll32.exe url.dll,FileProtocolHandler $target &} message]} { showError $message }
}

proc ::JLC2Capture::importPart {place} {
    variable selected
    variable busy
    variable placeRequested
    variable imported
    variable libraryRoot
    variable status
    variable operationPart
    variable operationRoot
    if {$busy || $selected eq ""} { return }
    if {$place && [llength [info commands IsSchematicViewActive]] && ![IsSchematicViewActive]} {
        showError "\u8bf7\u5148\u6253\u5f00\u5e76\u6fc0\u6d3b Capture \u539f\u7406\u56fe\u9875\u9762\u3002"
        return
    }
    set placeRequested $place
    set operationPart $selected
    set operationRoot [file normalize $libraryRoot]
    # The backend verifies current source and generated asset hashes. An
    # in-memory UI response is not evidence that disk files are unchanged.
    set status "\u6b63\u5728\u5bfc\u5165 $selected ..."
    start import [list --part $selected]
}

proc ::JLC2Capture::nativeFileFingerprint {path} {
    if {![llength [info commands zlib]]} { error "Capture runtime lacks zlib CRC32 for native library integrity checks" }
    if {![file isfile $path] || [file size $path] == 0} { error "Native library input is missing or empty: $path" }
    set stream [open $path rb]
    set crc 0
    set size 0
    set failed [catch {
        while {![eof $stream]} {
            set block [read $stream 65536]
            incr size [string length $block]
            set crc [zlib crc32 $block $crc]
        }
    } message options]
    close $stream
    if {$failed} { return -options $options $message }
    return [list $size [format %08x $crc]]
}

proc ::JLC2Capture::verifiedOlb {base xmlFingerprint} {
    set metadata "$base.integrity"
    if {![file isfile $metadata]} { return "" }
    set stream [open $metadata r]
    set failed [catch {set proof [read $stream 8192]} message options]
    close $stream
    if {$failed} { return -options $options $message }
    # This is data, never a script. A corrupted sidecar cannot direct reads
    # outside this generated Capture directory or at unrelated libraries.
    if {[dict get $proof format] != 1 || [dict get $proof xml] ne $xmlFingerprint} { return "" }
    set name [dict get $proof file]
    set stem [file rootname [file tail $base]]
    if {$name ne [file tail $name] || [file pathtype $name] ne "relative"
        || ($name ne [file tail $base] &&
            ([string first "$stem.verified." $name] != 0 || [file extension $name] ne ".olb"))} { return "" }
    set candidate [file join [file dirname $base] $name]
    if {[nativeFileFingerprint $candidate] ne [dict get $proof olb]} { return "" }
    return $candidate
}

proc ::JLC2Capture::saveOlbProof {base olb xmlFingerprint} {
    set proof [dict create format 1 file [file tail $olb] xml $xmlFingerprint olb [nativeFileFingerprint $olb]]
    set metadata "$base.integrity"
    set temporary "$metadata.[pid].[clock clicks].tmp"
    set stream [open $temporary {WRONLY CREAT EXCL}]
    set failed [catch {puts -nonewline $stream $proof; flush $stream} message options]
    close $stream
    if {$failed} {
        file delete $temporary
        return -options $options $message
    }
    if {[catch {file rename -force $temporary $metadata} message options]} {
        file delete $temporary
        return -options $options $message
    }
}

proc ::JLC2Capture::buildOlb {data} {
    set base [get $data olb_path]
    if {$base eq ""} { error "Backend response is missing olb_path" }
    set base [file normalize $base]
    set xml [get $data capture_xml]
    if {$xml eq "" || ![file exists $xml]} { error "Capture XML is missing: $xml" }
    set xmlFingerprint [nativeFileFingerprint $xml]
    if {![catch {verifiedOlb $base $xmlFingerprint} verified] && $verified ne ""} { return $verified }
    if {![llength [info commands XMATIC_XML2OLB]]} { error "XMATIC_XML2OLB is unavailable in this interpreter" }
    set olb $base
    if {[file exists $olb]} {
        # Capture can retain a library in memory. Preserve every existing OLB
        # byte-for-byte, and compile repairs to a new path for PlaceNew.
        set suffix [clock clicks]
        set olb "[file rootname $base].verified.[lindex $xmlFingerprint 1].$suffix.olb"
        while {[file exists $olb]} {
            incr suffix
            set olb "[file rootname $base].verified.[lindex $xmlFingerprint 1].$suffix.olb"
        }
    }
    set logPath [file rootname $olb].import.log
    set tclPath [file rootname $olb].import.tcl
    set result [catch {XMATIC_XML2OLB $xml $olb false $logPath $tclPath true} message]
    if {$result} { set result [catch {XMATIC_XML2OLB $xml $olb false $logPath $tclPath} message] }
    if {$result} { set result [catch {XMATIC_XML2OLB $xml $olb} message] }
    if {$result || ![file exists $olb] || [file size $olb] == 0} {
        error "Capture library import failed: $message\n$logPath"
    }
    if {[nativeFileFingerprint $xml] ne $xmlFingerprint} { error "Capture XML changed during native conversion; retry the import" }
    saveOlbProof $base $olb $xmlFingerprint
    return $olb
}

proc ::JLC2Capture::finishImport {data} {
    variable imported
    variable operationPart
    variable operationRoot
    variable placeRequested
    variable status
    variable window
    set responsePart [get $data part_id [get $data lcsc_id]]
    set responseRoot [get $data library_root]
    if {$responsePart eq "" || $responsePart ne $operationPart} {
        error "Import response part mismatch: expected $operationPart, received $responsePart"
    }
    if {$responseRoot eq ""} { error "Import response is missing library_root" }
    set responseRoot [file normalize $responseRoot]
    set sameRoot [expr {$responseRoot eq $operationRoot}]
    if {$::tcl_platform(platform) eq "windows"} { set sameRoot [string equal -nocase $responseRoot $operationRoot] }
    if {!$sameRoot} { error "Import response library folder does not match the requested folder" }
    if {![sourceExactResult $data]} { error "Import response conversion_policy must be source_exact; import this part again with the current converter" }
    validatePublishedAssets $data
    set olb [buildOlb $data]
    dict set data olb_path $olb
    registerFootprintPath $responseRoot [get $data footprint_name]
    set symbol [get $data symbol_name]
    if {$symbol eq ""} { error "Backend response is missing symbol_name" }
    set key [list $responseRoot $responsePart]
    if {[get $data step_status] eq "available"} {
        dict set imported $key $data
    } else {
        dict unset imported $key
    }
    set warnings [get $data warnings {}]
    set status "\u5df2\u5bfc\u5165 $responsePart   STEP: [get $data step_status unknown]"
    if {[llength $warnings]} { append status "  [join $warnings {; }]" }
    showDetails "$responsePart\nOLB: $olb\nPCB Footprint: [get $data footprint_name]\nSTEP: [get $data step_status unknown]\n\n[join $warnings \n]"
    showPreviews $data
    setBusy 0
    if {$placeRequested} {
        if {![llength [info commands PlaceNew]]} { error "PlaceNew is only available inside OrCAD Capture" }
        wm withdraw $window
        catch {UnSelectAll}
        PlaceNew $olb $symbol 0
    }
}

proc ::JLC2Capture::showError {message} {
    variable status
    variable window
    set status $message
    puts "JLC2Capture: $message"
    if {[llength [info commands tk_messageBox]] && [winfo exists $window]} {
        tk_messageBox -parent $window -title JLC/LCSC -icon error -type ok -message $message
    }
}

proc ::JLC2Capture::openBrowser {args} {
    if {[llength [info commands ::JLC2CaptureDock::available]] && [::JLC2CaptureDock::available]} {
        if {[catch {::JLC2CaptureDock::show} message]} { showError $message }
    } else {
        show
    }
}

set ::JLC2Capture::dockScript [file join $::JLC2Capture::pluginDir jlc2capture_dock.tcl]
if {[file isfile $::JLC2Capture::dockScript]} { source $::JLC2Capture::dockScript }
set ::JLC2Capture::updateScript [file join $::JLC2Capture::pluginDir jlc2capture_updates.tcl]
if {[file isfile $::JLC2Capture::updateScript]} { source $::JLC2Capture::updateScript }
::JLC2Capture::register
