--[[
  FA26 telemetry logger and channel probe.

  The 2026 car's physics Lua is obfuscated, so the energy model has to be
  measured from telemetry rather than read from source. Before that is possible
  we need to know which ERS channels are actually observable. This app probes a
  list of candidates, shows them live, and records whatever it finds.

  Every probe is wrapped in pcall: reading a field the build does not have
  raises rather than returning nil, and one bad name would otherwise take the
  whole app down.
]]

local CAR_MATCH = "vrc_formula_alpha_2026"

--: Written into every map sidecar, so a lap recorded by an older logger can
--: be told apart from one that carries its own map.
local LOGGER_VERSION = 3

--: The setup file the car loaded, which is the thing that actually settles
--: which deployment map a lap ran.
---
--- Reading the map itself out of the setup was tried first, through
--- `ac.getSetupSpinnerValue`, and it comes back zero for every deployment item
--- on this build -- the sidecars it wrote carried a correct strategy and an
--- empty map. The file name does not need the value of anything: the optimiser
--- can read a setup file perfectly well, and always could. What it never knew
--- was *which* file, which is the whole of the ambiguity this is here to
--- remove.
--- The index is inside the pcall as well as the call. On this build reaching
--- for a name `ac` does not have returns nil, but the header of this file says
--- it can raise, and a raise out here happens at load: no app, no laps, and a
--- line in a log nobody reads. One closure, once, at startup.
local setupFile = ""
pcall(function()
    ac.onSetupFile(function(_operation, filename)
        setupFile = filename or ""
    end)
end)

--- What the setup API returns, recorded once per lap so the question of why
--- it returns nothing does not cost another session on track to ask.
local function setupProbe()
    if not ac.getSetupSpinnerValue then return "absent" end
    -- `ARB_F` answers "does this work at all", and it does -- it came back
    -- 80000. `DEPLOYMENT_MAP_1_SPLIT_1_4` came back as the fallback, so the
    -- car's declared ID is not the key. The section name the value is stored
    -- under is the obvious next candidate, and `STRAT_MAP` is a third: it is
    -- a custom item like the deployment ones but an ordinary spinner in the
    -- setup screen, so it separates "custom items are not exposed" from
    -- "these particular items are not".
    local parts = {}
    for _, id in ipairs({ "DEPLOYMENT_MAP_1_SPLIT_1_4",
                          "CUSTOM_SCRIPT_ITEM_1052183907",
                          "STRAT_MAP", "ARB_F" }) do
        local ok, value = pcall(ac.getSetupSpinnerValue, id, -1)
        parts[#parts + 1] = id .. ":" .. (ok and tostring(value) or "raised")
    end
    return table.concat(parts, " ")
end

local MIN_SAMPLES = 200

local sim = ac.getSim()

-- Read live, never cached. Taken once at load this is whatever the sim
-- happened to report at that moment, which is not always the track that ends
-- up loaded -- and every distance written below is splinePosition times this
-- number, so a stale value scales an entire lap wrong. The optimiser then
-- builds a map in metres that do not exist, and the zones collapse.
local function trackLength()
  local length = sim.trackLengthM
  return (length and length > 0) and length or 0
end

local autoRecord = true
local skipPitLaps = true

local rows = {}
local elapsed = 0
local lastSpline = 0
local started = false
local sawPitlane = false
local sawInvalid = 0
--: Every strategy slot the wheel sat on during this lap, as a set. The driver
--: changes it mid-lap often enough that one number is not the answer.
local strats = {}

local lapsSaved = 0
local lastSavedName = ""
local lastSavedTime = 0
local lastSkipReason = ""
--: Laps finished but refused by the disk, and why. `io.save` does not raise
--: when Windows says no -- ransomware protection turning the game away from
--: Documents, most often -- so the count above went on rising over a folder
--: with nothing in it, and a driver with two laps "saved" had none.
local lapsLost = 0
local writeProblem = ""
local writeChecked = false
local savingTo = ""
--: A finished lap, held for one frame so the game can publish its time.
local pendingSave = nil

local sessionStamp = os.date("%Y%m%d_%H%M%S")

--- Candidate channels. Anything that stays flat at zero across a lap is not
--- available on this build and gets dropped from the next revision.
local probes = {
    { name = "speed_kmh",      get = function(c) return c.speedKmh end },
    { name = "gas",            get = function(c) return c.gas end },
    { name = "brake",          get = function(c) return c.brake end },
    { name = "rpm",            get = function(c) return c.rpm end },
    { name = "gear",           get = function(c) return c.gear end },
    { name = "turbo_boost",    get = function(c) return c.turboBoost end },
    { name = "lat_acc",        get = function(c) return c.acceleration.x end },
    { name = "long_acc",       get = function(c) return c.acceleration.z end },
    -- ERS candidates: the ones that matter for calibration
    { name = "kers_charge",    get = function(c) return c.kersCharge end },
    { name = "kers_current_kj", get = function(c) return c.kersCurrentKJ end },
    { name = "kers_input",     get = function(c) return c.kersInput end },
    { name = "kers_load",      get = function(c) return c.kersLoad end },
    { name = "drs_active",     get = function(c) return c.drsActive and 1 or 0 end },
    { name = "mguk_delivery",  get = function(c) return c.mgukDelivery end },
    { name = "mguk_recovery",  get = function(c) return c.mgukRecovery end },
    { name = "kers_charge_kj", get = function(c) return c.kersCharge * 4000 end },
    { name = "fuel",           get = function(c) return c.fuel end },
    -- Custom switches, probed for anything that tracks the active STRAT.
    -- ersPowerLevel, ersRecovery and ersHeatCharging were tried first and do
    -- not exist on this build. Of these, only extra_a and extra_e carry any
    -- value, and neither follows the strat, so which map is live still has to
    -- be identified from behaviour -- which is why the three maps are built to
    -- look nothing like each other.
    { name = "extra_a",        get = function(c) return c.extraA and 1 or 0 end },
    { name = "extra_b",        get = function(c) return c.extraB and 1 or 0 end },
    { name = "extra_c",        get = function(c) return c.extraC and 1 or 0 end },
    { name = "extra_d",        get = function(c) return c.extraD and 1 or 0 end },
    { name = "extra_e",        get = function(c) return c.extraE and 1 or 0 end },
    { name = "extra_f",        get = function(c) return c.extraF and 1 or 0 end },
}

--- Live values and whether each probe has ever produced a non-zero reading.
local live = {}
local available = {}
local everNonZero = {}
for i = 1, #probes do
    live[i] = 0
    available[i] = false
    everNonZero[i] = false
end

local function read(index, car)
    local ok, value = pcall(probes[index].get, car)
    if not ok or value == nil then return nil end
    if type(value) == "boolean" then return value and 1 or 0 end
    if type(value) ~= "number" then return nil end
    return value
end

local function header()
    local parts = { "t", "dist_m", "spline", "lap_valid" }
    for i = 1, #probes do
        if available[i] then parts[#parts + 1] = probes[i].name end
    end
    return table.concat(parts, ",")
end

local function trackTag()
    local track = ac.getTrackID()
    local layout = ac.getTrackLayout()
    if layout ~= nil and layout ~= "" then track = track .. "__" .. layout end
    return track
end

local function baseDir()
    local dir = ac.getFolder(ac.FolderID.ACDocuments) .. "/fa26_baseline"
    io.createDir(dir)
    savingTo = dir
    return dir
end

--- Write a file and say whether it is really there. Neither half is trusted
--- alone: `io.save` answers false, or nothing, when the write is refused, and
--- asking for the file afterwards is the answer that means it. A call this CSP
--- build does not have counts as no evidence rather than as a failure.
local function writeFile(path, data)
    local ok, result = pcall(io.save, path, data)
    if not ok or result == false then return false end
    local asked, exists = pcall(io.fileExists, path)
    if asked and exists == false then return false end
    return true
end

--- What the driver is told when the folder will not take a file.
local function refused(dir)
    return "Windows would not let the game write to " .. dir .. ". The usual "
        .. "cause is Windows Security's ransomware protection (Controlled "
        .. "folder access). Its Protection history names the program it "
        .. "blocked: allow that one, then drive another lap."
end

--- Once, before any lap, so a blocked folder shows while the driver is still
--- in the pits rather than after a session of laps that went nowhere.
local function checkWritable()
    local dir = baseDir()
    local probe = dir .. "/write_check.tmp"
    if writeFile(probe, "fa26") then
        pcall(io.deleteFile, probe)
    else
        writeProblem = refused(dir)
    end
end

local function formatLapTime(seconds)
    local minutes = math.floor(seconds / 60)
    return string.format("%dm%06.3fs", minutes, seconds - minutes * 60)
end

local function resetLap()
    rows = {}
    elapsed = 0
    sawPitlane = false
    sawInvalid = 0
    strats = {}
end

--- How many splits and fields a deployment map has, and how many maps exist.
--- Mirrors cars/fa26.py; if the car ever gains more, both move together.
local MAPS = 12
local SPLITS_PER_MAP = 24
local FIELDS_PER_SPLIT = 6

--- The deployment map the car is running, read out of the setup rather than
--- inferred afterwards.
---
--- This is the whole point of the file it writes. Attribution used to be a
--- reconstruction: take the newest setup snapshot written before the lap, on
--- the assumption the lap was driven on STRAT 1, and hope. Of 178 recorded
--- laps only 36 survived that, and a quarter of the failures were laps driven
--- on a different strategy entirely. None of it was a modelling problem. It
--- was not writing down what the car was doing at the time.
---
--- `ac.getSetupSpinnerValue` returns the live value of a setup item by its
--- declared ID, which is exactly what a deployment map is made of, so there is
--- nothing to infer. Wrapped, because a CSP without it should cost a sidecar
--- file rather than the lap.
local function readMap(index)
    local splits = {}
    for s = 1, SPLITS_PER_MAP do
        local values = {}
        local live_split = false
        for f = 1, FIELDS_PER_SPLIT do
            local id = string.format("DEPLOYMENT_MAP_%d_SPLIT_%d_%d", index, s, f)
            local ok, value = pcall(ac.getSetupSpinnerValue, id, 0)
            value = (ok and type(value) == "number") and value or 0
            values[f] = string.format("%d", math.floor(value + 0.5))
            if value ~= 0 then live_split = true end
        end
        -- An empty split is six zeroes and says nothing; only live ones are
        -- written, so a map with two zones is two lines rather than twenty-four.
        if live_split then
            splits[#splits + 1] = string.format("SPLIT_%d=%s", s, table.concat(values, ","))
        end
    end
    return splits
end

--- Everything needed to attribute this lap, beside the lap.
local function saveMaps(path, lap)
    local seen = {}
    for slot in pairs(lap.strats) do seen[#seen + 1] = slot end
    table.sort(seen)
    if #seen == 0 then return false end

    local out = {
        "; The deployment maps live while this lap was recorded, read from the",
        "; setup as it finished. Written by the logger so the lap needs no",
        "; guessing about which map it ran.",
        "[LAP]",
        "LOGGER=" .. tostring(LOGGER_VERSION),
        string.format("TRACK_LENGTH_M=%.1f", trackLength()),
        "STRATS=" .. table.concat(seen, ","),
        "DOMINANT_STRAT=" .. tostring(lap.dominant or seen[1]),
        "INVALID_SAMPLES=" .. tostring(lap.sawInvalid or 0),
        "SETUP_FILE=" .. tostring(lap.setupFile or ""),
        "SETUP_PROBE=" .. tostring(lap.setupProbe or ""),
    }
    for _, slot in ipairs(seen) do
        if slot >= 1 and slot <= MAPS then
            out[#out + 1] = string.format("[MAP_%d]", slot)
            local splits = readMap(slot)
            for _, line in ipairs(splits) do out[#out + 1] = line end
        end
    end
    io.save(path, table.concat(out, "\n") .. "\n")
    return true
end

--- Declared once rather than built per frame. The strategy is read through
--- pcall like every other channel, and a closure written inline there would be
--- a fresh allocation 120 times a second -- CSP logged a 31 ms hitch in
--- `update()` on the session that first ran it.
local function readDelivery(car)
    return car.mgukDelivery
end

--- Everything the sidecar wants that is not the lap itself, behind one call.
---
--- Every line of this touches the game's API, and every one of them can raise
--- -- CSP objects raise on a field they do not have rather than returning nil,
--- which this file's own header says and which was then ignored one function
--- along. It exists as a single function so that the one place it is called
--- from can wrap it in a single pcall, and so that no future addition ends up
--- outside that wrapping by being written in line.
local function sidecarExtras()
    local file = setupFile
    if file == "" then
        local config = ac.INIConfig.currentSetup()
        if config and config.filename then file = config.filename end
    end
    return file, setupProbe()
end

local function sample(car, dt)
    elapsed = elapsed + dt
    if car.isInPitlane then sawPitlane = true end
    if not car.isLapValid then sawInvalid = sawInvalid + 1 end
    -- setup_ers_strat_maps.lut is zero-based, so delivery 0 is STRAT 1.
    --
    -- Read through pcall like every other channel, and for the same reason
    -- this file opens by explaining: a field the build does not have raises
    -- rather than returning nil. This one runs every frame, so an unguarded
    -- read on a build without it would not lose a channel, it would take the
    -- whole app down and record nothing at all.
    local ok, delivery = pcall(readDelivery, car)
    local slot = math.floor(((ok and delivery or 0)) + 1.5)
    strats[slot] = (strats[slot] or 0) + 1

    local parts = {
        string.format("%.4f", elapsed),
        string.format("%.3f", car.splinePosition * trackLength()),
        string.format("%.6f", car.splinePosition),
        car.isLapValid and "1" or "0",
    }
    for i = 1, #probes do
        local v = read(i, car)
        if v ~= nil then
            live[i] = v
            available[i] = true
            if math.abs(v) > 1e-6 then everNonZero[i] = true end
            parts[#parts + 1] = string.format("%.7f", v)
        elseif available[i] then
            parts[#parts + 1] = "0"
        end
    end
    rows[#rows + 1] = table.concat(parts, ",")
end

local function saveLap(lap, officialTime)
    if #lap.rows < MIN_SAMPLES then
        lastSkipReason = "lap too short (" .. #lap.rows .. " samples)"
        return
    end
    if lap.sawPitlane and skipPitLaps then
        lastSkipReason = "in/out lap (pitlane) -- turn off 'Skip in/out laps' to keep these"
        return
    end
    -- A lap the stewards did not like is still a lap that happened, and this
    -- app's job is to record what happened. There used to be a switch here
    -- that threw those laps away, on by default, and it was eating the good
    -- ones: a push lap out of a recharge lap where a corner was cut arrived
    -- with the game still reporting the previous lap invalid, so the push lap
    -- -- clean, quick, the whole point of the run -- was discarded without a
    -- word. Several 1:38s at Austin went that way.
    --
    -- Nothing is lost by keeping them. `lap_valid` is recorded per sample in
    -- the file itself and the count goes in the sidecar, so anything that
    -- wants to exclude an invalidated lap still can -- visibly, and after the
    -- fact, where the decision can be seen and undone. The recording end does
    -- not get to make that call.

    -- The game's own time when there is one and it agrees with ours to within
    -- a tenth. Ours is one frame short by construction, so a bigger gap than
    -- that means the value belongs to a different lap and is no use here.
    local shown = lap.elapsed
    if officialTime and officialTime > 0
       and math.abs(officialTime - lap.elapsed) < 0.1 then
        shown = officialTime
    end

    local name = string.format("%s__%s__%s__lap%02d_%s.csv",
        trackTag(), ac.getCarID(0), sessionStamp, lapsSaved + 1,
        formatLapTime(shown) .. (lap.sawPitlane and "_pit" or ""))
    local dir = baseDir()
    if not writeFile(dir .. "/" .. name,
            header() .. "\n" .. table.concat(lap.rows, "\n") .. "\n") then
        lapsLost = lapsLost + 1
        writeProblem = refused(dir)
        lastSkipReason = ""
        return
    end
    writeProblem = ""
    -- Beside the lap, the maps it was driven on. Same name, different
    -- suffix, so nothing looking for laps picks it up.
    pcall(saveMaps, dir .. "/" .. name:gsub("%.csv$", ".map"), lap)

    lapsSaved = lapsSaved + 1
    lastSavedName = name
    lastSavedTime = shown
    lastSkipReason = ""
end

function script.update(dt)
    local car = ac.getCar(0)
    if car == nil or dt <= 0 then return end

    if not writeChecked then
        writeChecked = true
        pcall(checkWritable)
    end

    -- Keep the probe display alive even when not recording, so channels can be
    -- checked in the pits without driving a full lap.
    for i = 1, #probes do
        local v = read(i, car)
        if v ~= nil then
            live[i] = v
            available[i] = true
            if math.abs(v) > 1e-6 then everNonZero[i] = true end
        end
    end

    local spline = car.splinePosition
    local crossedLine = spline < 0.1 and lastSpline > 0.9
    lastSpline = spline

    if not autoRecord then
        started = false
        return
    end

    -- Written one frame after the crossing, not on it: reading the finished
    -- lap's time on the crossing frame itself can still return the lap
    -- before it.
    if pendingSave then
        local official = nil
        if car.previousLapTimeMs and car.previousLapTimeMs > 0 then
            official = car.previousLapTimeMs / 1000.0
        end
        saveLap(pendingSave, official)
        pendingSave = nil
    end

    if crossedLine then
        if started then
            -- Which strategy the wheel spent most of this lap on, for the
            -- sidecar. `strats` counts frames per slot; `resetLap` rebinds it
            -- rather than clearing it, so the table handed over here stays
            -- this lap's.
            local dominant, most = nil, -1
            for slot, count in pairs(strats) do
                if count > most then dominant, most = slot, count end
            end
            -- One pcall around everything the sidecar wants, and nothing of
            -- the game's touched outside it. `config.filename` was read bare
            -- here, and indexing a field a CSP object does not have raises
            -- rather than returning nil -- on the one frame per lap that
            -- decides whether the lap is saved at all. The app went on
            -- running, the window said what it always says, and a session
            -- came and went with `Laps saved: 0`.
            --
            -- The lap is the thing worth having. The sidecar is worth having
            -- too, and it is never worth the lap.
            local ok, file, probe = pcall(sidecarExtras)
            pendingSave = { rows = rows, elapsed = elapsed,
                            sawPitlane = sawPitlane, sawInvalid = sawInvalid,
                            strats = strats, dominant = dominant,
                            setupFile = ok and file or setupFile,
                            setupProbe = ok and probe or "raised" }
        end
        started = true
        resetLap()
        return
    end

    if started then sample(car, dt) end
end

function windowMain()
    local id = ac.getCarID(0)
    if not id:find(CAR_MATCH) then
        ui.textColored("Not an FA26 (" .. id .. ")", rgbm(1, 0.5, 0.4, 1))
        return
    end

    ui.text("Track:  " .. ac.getTrackID())
    ui.text(string.format("Length: %.1f m", trackLength()))
    ui.separator()

    -- First, above everything: a logger that cannot save is not a logger.
    if writeProblem ~= "" then
        ui.textColored("LAPS ARE NOT BEING SAVED", rgbm(1, 0.35, 0.3, 1))
        ui.textWrapped(writeProblem)
        ui.separator()
    end

    if ui.button(autoRecord and "Auto-record: ON" or "Auto-record: OFF", vec2(330, 26)) then
        autoRecord = not autoRecord
        if not autoRecord then resetLap() end
    end
    -- Out laps were being thrown away, which is fine until the thing you need
    -- to explain happens on one.
    if ui.button(skipPitLaps and "Skip in/out laps: ON" or "Skip in/out laps: OFF",
        vec2(330, 22)) then
        skipPitLaps = not skipPitLaps
    end

    if not autoRecord then
        ui.textColored("Paused", rgbm(0.8, 0.8, 0.8, 1))
    elseif started then
        ui.textColored(string.format("RECORDING  %d samples  %.1f s", #rows, elapsed),
            rgbm(0.3, 1, 0.3, 1))
    else
        ui.textColored("Waiting for the start line", rgbm(1, 0.8, 0.2, 1))
    end

    ui.separator()
    ui.text("CHANNEL PROBE")
    ui.text("green = live data, grey = always zero, red = not on this build")
    ui.separator()

    for i = 1, #probes do
        local label = string.format("%-16s %12.4f", probes[i].name, live[i])
        if not available[i] then
            ui.textColored(string.format("%-16s   unavailable", probes[i].name),
                rgbm(1, 0.45, 0.4, 1))
        elseif everNonZero[i] then
            ui.textColored(label, rgbm(0.35, 1, 0.35, 1))
        else
            ui.textColored(label, rgbm(0.65, 0.65, 0.65, 1))
        end
    end

    ui.separator()
    ui.text(string.format("Laps saved: %d", lapsSaved))
    if lapsLost > 0 then
        ui.textColored(string.format("Laps not saved: %d", lapsLost), rgbm(1, 0.35, 0.3, 1))
    end
    if savingTo ~= "" then ui.textWrapped("Saving to " .. savingTo) end
    if lastSavedTime > 0 then ui.text("Last lap: " .. formatLapTime(lastSavedTime)) end
    if lastSavedName ~= "" then ui.textWrapped(lastSavedName) end
    if lastSkipReason ~= "" then
        ui.textColored("Skipped: " .. lastSkipReason, rgbm(1, 0.6, 0.3, 1))
    end
end
