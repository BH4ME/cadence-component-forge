# Capture's supported CEF control bar; all worker commands remain fixed and local.
namespace eval ::JLC2CaptureDock {
    variable widget 0
    variable widgetType "dock"
    # Keep the component browser in Capture's right control-bar.  Older
    # revisions converted a narrow bar to an MDI tab, which made the panel
    # appear to disappear from the right taskbar on machines with a saved
    # narrow Capture layout.  MDI conversion is now opt-in through config.
    variable rightDock 1
    variable target [dict create]
    variable channel ""
    variable responseFile ""
    variable operation ""
    variable operationPart ""
    variable operationRoot ""
    variable operationId ""
    variable operationTarget [dict create]
    variable outputText ""
    variable lastImport [dict create]
    variable fallbackReason ""
    variable queuedRequest {}
}

proc ::JLC2CaptureDock::cefAvailable {} {
    foreach command {
        CreateControlBar LoadResource ShowWidget GetWidth Execute DestroyWidget
        CreateWidget SetTitle MDIActivate RegisterCloseTclFn DeRegisterCloseTclFn
    } {
        if {![llength [info commands ::orPrm::$command]]} { return 0 }
    }
    foreach constant {WDOCK_RIGHT WDALLOW_RIGHT WF_CEF WTYPE_VIEW} {
        if {![info exists ::orPrm::$constant] || [set ::orPrm::$constant] eq ""} { return 0 }
    }
    return 1
}

proc ::JLC2CaptureDock::available {} {
    if {[cefAvailable]} { return 1 }
    catch {package require orPrmWebComp}
    if {[cefAvailable]} { return 1 }
    catch {load orPrmWebCompIE64 orPrmWebComp}
    return [cefAvailable]
}

proc ::JLC2CaptureDock::nativeFallback {reason} {
    variable fallbackReason
    set fallbackReason $reason
    closed
    puts "JLC2Capture: embedded browser unavailable; using native search ($reason)"
    ::JLC2Capture::show
}

proc ::JLC2CaptureDock::fileUri {path} {
    set path [string map {\\ /} [file normalize $path]]
    binary scan [encoding convertto utf-8 $path] H* result
    # Encode UTF-8 bytes without invoking a shell or interpreting path contents.
    set encoded ""
    foreach {a b} [split $result ""] {
        scan "$a$b" %x code
        set char [format %c $code]
        if {[regexp {^[A-Za-z0-9/_.:~-]$} $char]} { append encoded $char } else { append encoded %[string toupper "$a$b"] }
    }
    if {[string match //* $encoded]} { return "file:$encoded" }
    return "file:///$encoded"
}

proc ::JLC2CaptureDock::show {args} {
    rememberTarget
    ::JLC2Capture::configure
    if {![available]} { nativeFallback "Required CEF capabilities are missing"; return }
    if {[catch {showCef} message]} { nativeFallback $message }
}

proc ::JLC2CaptureDock::showCef {} {
    variable widget
    variable widgetType
    variable rightDock
    set useMdi 0
    if {[dict exists $::JLC2Capture::configuration use_mdi_fallback]} {
        set useMdi [expr {[::JLC2Capture::get $::JLC2Capture::configuration use_mdi_fallback 0] in {1 true}}]
    }
    # Right docking is the default. A site that must preserve the legacy MDI
    # tab can opt in with use_mdi_fallback=true in config.json.
    set rightDock [expr {!$useMdi}]
    if {$widget != 0} {
        # A session loaded from a pre-0.47 initializer may still contain the
        # old MDI view. Recreate it as a right control bar on the next open.
        if {$widgetType eq "view" && $rightDock} {
            set old $widget
            set widget 0
            set widgetType dock
            catch {::orPrm::DeRegisterCloseTclFn $old}
            catch {::orPrm::DestroyWidget $old}
        } else {
            ::orPrm::ShowWidget $widget 1
            if {$widgetType eq "view"} { ::orPrm::MDIActivate $widget; return }
            return
        }
    }
    set directory $::JLC2Capture::pluginDir
    set source [open [file join $directory web index.html] r]
    fconfigure $source -encoding utf-8
    set failed [catch {set html [read $source]} message options]
    close $source
    if {$failed} { return -options $options $message }
    set cadence [fileUri $::JLC2Capture::cadenceRoot]
    set assets [fileUri [file join $directory web]]
    set html [string map [list __CADENCE_ROOT__ $cadence __PLUGIN_ASSETS__ $assets] $html]
    set runtime [file join $::JLC2Capture::libraryRoot .capture_jobs]
    file mkdir $runtime
    set path [file join $runtime "dock_[pid].html"]
    set stream [open $path w]
    fconfigure $stream -encoding utf-8
    set failed [catch {puts -nonewline $stream $html} message options]
    close $stream
    if {$failed} { return -options $options $message }
    if {$widget == 0} {
        set widget [::orPrm::CreateControlBar "JLC / LCSC" $::orPrm::WDOCK_RIGHT $::orPrm::WDALLOW_RIGHT 400 $::orPrm::WF_CEF]
        if {$widget eq "" || $widget == 0} { set widget 0; error "CEF control bar creation failed" }
        ::orPrm::SetTitle $widget "JLC / LCSC"
        # Capture 17.2/17.4 expose the two-argument LoadResource contract.
        # A third "reload" flag is not part of that API and makes the whole
        # browser fall back to Tk on installations that enforce the signature.
        ::orPrm::LoadResource $widget [file nativename $path]
        ::orPrm::ShowWidget $widget 1
        set widgetType dock
    }
    # Capture can restore a right bar narrower than the browser's preferred
    # width. Keep it docked and let panel.css reflow at that width. MDI is
    # available only when explicitly requested for legacy layouts.
    if {!$rightDock && $useMdi && [::orPrm::GetWidth $widget] < 330} {
        ::orPrm::ShowWidget $widget 0
        ::orPrm::DestroyWidget $widget
        set widget 0
        set widget [::orPrm::CreateWidget $::orPrm::WTYPE_VIEW $::orPrm::WF_CEF]
        if {$widget eq "" || $widget == 0} { set widget 0; error "CEF view creation failed" }
        set widgetType view
        ::orPrm::SetTitle $widget "JLC / LCSC"
        ::orPrm::LoadResource $widget [file nativename $path]
        ::orPrm::RegisterCloseTclFn $widget ::JLC2CaptureDock::closed
        ::orPrm::ShowWidget $widget 1
    }
}

proc ::JLC2CaptureDock::closed {args} {
    variable widget
    variable widgetType
    variable queuedRequest
    set queuedRequest {}
    if {$widget == 0} { return }
    set closing $widget
    set widget 0
    set widgetType dock
    catch {::orPrm::DeRegisterCloseTclFn $closing}
    catch {::orPrm::DestroyWidget $closing}
}

proc ::JLC2CaptureDock::currentTarget {} {
    if {![llength [info commands ::IsSchematicViewActive]] || ![::IsSchematicViewActive]} {
        ::error "Activate a schematic page before placing"
    }
    set project [::GetActiveOpjName]
    set frame [::DboTclHelper_sGetConstCharPtr [::capGetMostRecentSchFrameID]]
    if {$project eq "" || $frame eq "" || $frame eq "0"} {
        ::error "The destination schematic page is unavailable"
    }
    return [dict create frame $frame project [file normalize $project]]
}

proc ::JLC2CaptureDock::rememberTarget {} {
    variable target
    if {[llength [info commands ::IsSchematicViewActive]] && [::IsSchematicViewActive]} {
        set target [dict create]
        set target [currentTarget]
    }
}

proc ::JLC2CaptureDock::frameIdentity {frame} {
    if {[regexp {^(.*\))\*$} $frame -> clean]} { return $clean }
    return $frame
}

proc ::JLC2CaptureDock::activateTarget {destination} {
    if {![dict exists $destination frame] || ![dict exists $destination project]} {
        ::error "Activate a schematic page and reopen JLC / LCSC before placing"
    }
    set frame [dict get $destination frame]
    set project [dict get $destination project]
    if {$frame eq "" || $frame eq "0" || $project eq ""} {
        ::error "The destination schematic page is unavailable"
    }
    # Capture's frame identifier is a title; edits add a single trailing '*'.
    set identity [frameIdentity $frame]
    set candidates [list $frame]
    if {[string index $identity end] eq ")"} {
        lappend candidates [expr {$identity eq $frame ? "${identity}*" : $identity}]
    }
    foreach candidate $candidates {
        if {[catch {::ui::SchematicActivate $candidate}]} { continue }
        if {[catch {currentTarget} active]} { continue }
        if {[string equal -nocase [dict get $active project] $project] && [frameIdentity [dict get $active frame]] eq $identity} { return }
    }
    ::error "The destination schematic page is closed or no longer available"
}

proc ::JLC2CaptureDock::placementTarget {} {
    variable target
    rememberTarget
    activateTarget $target
    return $target
}

proc ::JLC2CaptureDock::emit {kind jsonText {requestId ""}} {
    variable widget
    if {$widget == 0} { return }
    set payload [::JLC2Capture::encodeBase64 $jsonText]
    set correlation ""
    if {$requestId ne ""} {
        if {![string is wideinteger -strict $requestId] || $requestId < 0} { ::error "Invalid request ID" }
        set correlation ",[expr {wide($requestId)}]"
    }
    ::orPrm::Execute $widget "JLC2Dock.receive('$kind','$payload'$correlation)"
}

proc ::JLC2CaptureDock::reportError {message {requestId ""}} {
    package require json::write
    emit error [::json::write object message [::json::write string $message]] $requestId
}

proc ::JLC2CaptureDock::request {kind payload {clientId ""}} {
    variable channel
    variable lastImport
    if {$kind ni {init search preview preview3d import place folder step updates}} { return "unsupported" }
    if {[string length $payload] > 8192 || ![regexp {^[A-Za-z0-9+/=]*$} $payload]} { return "invalid" }
    set requestId ""
    set result "accepted"
    if {[catch {
        package require json
        package require json::write
        set jsonText [string trim [::JLC2Capture::decodeBase64 $payload]]
        if {[string index $jsonText 0] ne "\{" || ![::json::validate $jsonText]} { ::error "Request must be a JSON object" }
        # Tcllib 1.15 json2dict accepts trailing values; require full parser consumption.
        set tokens [regexp -all -inline -- $::json::tokenRE $jsonText]
        set cursor 0
        set data [::json::parseValue $tokens [llength $tokens] cursor]
        if {$cursor != [llength $tokens]} { ::error "Request contains trailing JSON values" }
        set candidateId [::JLC2Capture::get $data request_id ""]
        if {$candidateId ne "" && (![string is wideinteger -strict $candidateId] || $candidateId < 0 || $candidateId > 9007199254740991)} {
            ::error "Invalid request ID"
        }
        if {$candidateId ne ""} { set requestId [expr {wide($candidateId)}] }
        if {$kind eq "init"} {
            emit init [::json::write object library_root [::json::write string $::JLC2Capture::libraryRoot] \
                path_warning [::json::write string $::JLC2Capture::pathWarning]] $requestId
            set result "ready"
        } elseif {$channel ne "" && $kind ni {search preview preview3d}} {
            set result "busy"
        } else {
          switch -- $kind {
            updates { ::JLC2CaptureUpdates::show }
            folder { ::JLC2Capture::openRoot }
            step {
                if {![::JLC2Capture::sourceExactResult $lastImport]} {
                    set lastImport [dict create]
                    ::error "The previous component was not converted with source_exact; preview or import it again"
                }
                set ::JLC2Capture::stepFiles [::JLC2Capture::get $lastImport step_files {}]
                set ::JLC2Capture::stepViewerUrl [::JLC2Capture::get $lastImport step_viewer_url]
                ::JLC2Capture::viewStep
            }
            search {
                set query [string trim [::JLC2Capture::get $data query]]
                set page [::JLC2Capture::get $data page 1]
                if {[string length $query] < 1 || [string length $query] > 200} { ::error "Invalid search query" }
                if {![string is integer -strict $page] || $page < 1 || $page > 100000} { ::error "Invalid search page" }
                set result [start search [list --query $query --page $page --limit 30] $requestId]
            }
            default {
                set part [string toupper [::JLC2Capture::get $data part]]
                if {![regexp {^C[0-9]{3,}$} $part]} { ::error "Invalid LCSC part number" }
                set arguments [list --part $part]
                if {$kind in {preview preview3d}} { lappend arguments --prepare-only }
                if {$kind eq "preview"} { lappend arguments --defer-step }
                if {$kind eq "place"} {
                    set result [start $kind $arguments $requestId [placementTarget]]
                } else {
                    set result [start $kind $arguments $requestId]
                }
            }
          }
        }
    } message]} { reportError $message $requestId; return "error" }
    return $result
}

proc ::JLC2CaptureDock::start {kind arguments {requestId ""} {destination {}}} {
    variable channel
    variable operation
    variable operationPart
    variable operationRoot
    variable responseFile
    variable outputText
    variable operationId
    variable operationTarget
    variable queuedRequest
    if {$channel ne ""} {
        if {$kind in {search preview preview3d} && $operation in {search preview preview3d}} {
            # Only the newest selection matters. Let the in-flight read-only
            # preparation finish safely, then discard its obsolete response.
            set queuedRequest [list $kind $arguments $requestId $destination]
            return "queued"
        }
        return "busy"
    }
    set operationTarget [dict create]
    if {$kind eq "place"} {
        if {$destination eq ""} { set destination [placementTarget] }
        set operationTarget $destination
    }
    set operationId $requestId
    set operation $kind
    set operationRoot [file normalize $::JLC2Capture::libraryRoot]
    set operationPart ""
    if {$kind ne "search"} { set operationPart [lindex $arguments 1] }
    set outputText ""
    set commandKind [expr {$kind eq "search" ? "search" : "import"}]
    set directory [file join $operationRoot .capture_jobs]
    file mkdir $directory
    set responseFile [file join $directory "dock_[pid]_[clock microseconds].json"]
    # Use the paired pythonw runtime on Windows so an asynchronous Capture
    # search/import never creates a console window.  The response file remains
    # the protocol; the worker does not need stdout.
    set command [list [::JLC2Capture::workerPython] -u -m jlc2cadence.capture_bridge $commandKind {*}$arguments --library-root $operationRoot --output $responseFile]
    if {$kind ne "search"} {
        if {$::JLC2Capture::cadenceRoot ne ""} { lappend command --cadence-root $::JLC2Capture::cadenceRoot }
        if {$::JLC2Capture::viewerBaseUrl ne ""} { lappend command --viewer-base-url $::JLC2Capture::viewerBaseUrl }
    }
    set saved [dict create]
    foreach key {PYTHONPATH PYTHONIOENCODING} {
        if {[info exists ::env($key)]} { dict set saved $key $::env($key) }
    }
    set ::env(PYTHONPATH) [file join $::JLC2Capture::projectRoot src]
    if {[dict exists $saved PYTHONPATH] && [dict get $saved PYTHONPATH] ne ""} {
        append ::env(PYTHONPATH) [expr {$::tcl_platform(platform) eq "windows" ? ";" : ":"}] [dict get $saved PYTHONPATH]
    }
    set ::env(PYTHONIOENCODING) utf-8
    set failed [catch {set channel [open [concat [list |] $command [list 2>@1]] r]} message options]
    foreach key {PYTHONPATH PYTHONIOENCODING} {
        if {[dict exists $saved $key]} { set ::env($key) [dict get $saved $key] } else { unset ::env($key) }
    }
    if {$failed} { return -options $options $message }
    fconfigure $channel -blocking 0 -encoding utf-8 -translation auto
    fileevent $channel readable ::JLC2CaptureDock::receive
    return "accepted"
}

proc ::JLC2CaptureDock::startQueued {} {
    variable queuedRequest
    if {![llength $queuedRequest]} { return 0 }
    set arguments $queuedRequest
    set queuedRequest {}
    if {[catch {start {*}$arguments} message]} {
        reportError $message [lindex $arguments 2]
    }
    return 1
}

proc ::JLC2CaptureDock::receive {} {
    variable channel
    variable outputText
    variable responseFile
    variable operation
    variable operationId
    if {$channel eq ""} { return }
    if {[catch {read $channel} text]} {
        catch {fileevent $channel readable {}}
        catch {close $channel}
        set channel ""
        if {[startQueued]} { return }
        reportError $text $operationId
        return
    }
    append outputText $text
    if {[string length $outputText] > 65536} { set outputText [string range $outputText end-65535 end] }
    if {![eof $channel]} { return }
    fileevent $channel readable {}
    set failed [catch {close $channel} closeMessage]
    set channel ""
    if {[startQueued]} { return }
    if {[catch {
        set data [::JLC2Capture::readJson $responseFile]
        if {![::JLC2Capture::get $data ok 0]} { ::error [::JLC2Capture::backendError $data] }
        if {$failed} { ::error "$closeMessage\n$outputText" }
        finish $data
    } message]} { reportError $message $operationId }
}

proc ::JLC2CaptureDock::finish {data} {
    variable operation
    variable operationPart
    variable operationRoot
    variable responseFile
    variable lastImport
    variable operationId
    variable operationTarget
    if {$operation ne "search"} {
        if {[::JLC2Capture::get $data part_id] ne $operationPart} { ::error "Import response part mismatch" }
        set responseRoot [::JLC2Capture::get $data library_root]
        if {$responseRoot eq ""} { ::error "Import response is missing library_root" }
        set root [file normalize $responseRoot]
        if {![string equal -nocase $root $operationRoot]} { ::error "Import response library folder mismatch" }
        if {![::JLC2Capture::sourceExactResult $data]} {
            set lastImport [dict create]
            ::error "Import response conversion_policy must be source_exact; import this part again with the current converter"
        }
        if {$operation ni {preview preview3d}} {
            ::JLC2Capture::validatePublishedAssets $data
            dict set data olb_path [::JLC2Capture::buildOlb $data]
            ::JLC2Capture::registerFootprintPath $root [::JLC2Capture::get $data footprint_name]
        }
        set lastImport $data
    }
    set stream [open $responseFile r]
    fconfigure $stream -encoding utf-8
    set failed [catch {set json [read $stream]} message options]
    close $stream
    if {$failed} { return -options $options $message }
    if {$operation eq "place"} {
        if {![llength [info commands PlaceNew]]} { ::error "PlaceNew requires OrCAD Capture" }
        activateTarget $operationTarget
        catch {UnSelectAll}
        PlaceNew [dict get $data olb_path] [dict get $data symbol_name] 0
    }
    emit $operation $json $operationId
}
