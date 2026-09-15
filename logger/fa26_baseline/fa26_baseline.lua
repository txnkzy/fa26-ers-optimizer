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
local onlyValidLaps = true
local skipPitLaps = true

local rows = {}
local elapsed = 0
local lastSpline = 0
local started = false
local sawPitlane = false
local sawInvalid = false

local lapsSaved = 0
local lastSavedName = ""
local lastSavedTime = 0
local lastSkipReason = ""

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
    return dir
end

local function formatLapTime(seconds)
    local minutes = math.floor(seconds / 60)
    return string.format("%dm%06.3fs", minutes, seconds - minutes * 60)
end

local function resetLap()
    rows = {}
    elapsed = 0
    sawPitlane = false
    sawInvalid = false
end

local function sample(car, dt)
    elapsed = elapsed + dt
    if car.isInPitlane then sawPitlane = true end
    if not car.isLapValid then sawInvalid = true end

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

local function saveLap()
    if #rows < MIN_SAMPLES then
        lastSkipReason = "lap too short (" .. #rows .. " samples)"
        return
    end
    if sawPitlane and skipPitLaps then
        lastSkipReason = "in/out lap (pitlane) -- turn off 'Skip in/out laps' to keep these"
        return
    end
    if onlyValidLaps and sawInvalid then
        lastSkipReason = "lap invalidated"
        return
    end

    local name = string.format("%s__%s__%s__lap%02d_%s.csv",
        trackTag(), ac.getCarID(0), sessionStamp, lapsSaved + 1,
        formatLapTime(elapsed) .. (sawPitlane and "_pit" or ""))
    io.save(baseDir() .. "/" .. name, header() .. "\n" .. table.concat(rows, "\n") .. "\n")

    lapsSaved = lapsSaved + 1
    lastSavedName = name
    lastSavedTime = elapsed
    lastSkipReason = ""
end

function script.update(dt)
    local car = ac.getCar(0)
    if car == nil or dt <= 0 then return end

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

    if crossedLine then
        if started then saveLap() end
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

    if ui.button(autoRecord and "Auto-record: ON" or "Auto-record: OFF", vec2(330, 26)) then
        autoRecord = not autoRecord
        if not autoRecord then resetLap() end
    end
    if ui.button(onlyValidLaps and "Only valid laps: ON" or "Only valid laps: OFF",
        vec2(330, 22)) then
        onlyValidLaps = not onlyValidLaps
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
    if lastSavedTime > 0 then ui.text("Last lap: " .. formatLapTime(lastSavedTime)) end
    if lastSavedName ~= "" then ui.textWrapped(lastSavedName) end
    if lastSkipReason ~= "" then
        ui.textColored("Skipped: " .. lastSkipReason, rgbm(1, 0.6, 0.3, 1))
    end
end
