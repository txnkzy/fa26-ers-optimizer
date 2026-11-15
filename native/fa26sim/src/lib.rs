//! The FA26 whole-lap integration, compiled.
//!
//! A line-for-line port of the loop in `core/sim26.py` (`simulate_py`) and the
//! helpers it calls: `fa26.Powertrain.deploy_kw`, `fa26.curve_kw`,
//! `fa26.harvest_kw_at`, `CarPhysics._lookup` and `sim26._commands_along`.
//! The Python stays the reference; this must give the same numbers, so every
//! expression keeps the Python's operand order, and comparisons are written
//! the way Python's `min`/`max` resolve them rather than with `f64::min`.
//!
//! Square roots go through the `pow` the caller passes in. Python's `x ** 0.5`
//! is the C runtime's `pow`, which is not always the correctly rounded square
//! root, so using `sqrt` here would differ in the last bit now and then.

use std::slice;

type PowFn = unsafe extern "C" fn(f64, f64) -> f64;

// Indices into the `p` (f64) parameter block.
const P_STEP: usize = 0;
const P_MASS: usize = 1;
const P_TRACTION_FORCE: usize = 2;
const P_FLOOR_KW: usize = 3;
const P_FLOOR_HOLD_S: usize = 4;
const P_RAMP_DOWN: usize = 5;
const P_START_STORE: usize = 6;
const P_HARVEST_CAP: usize = 7;
const P_CAPACITY: usize = 8;
const P_MAX_HARVEST: usize = 9;
const P_MOTOR_EFF: usize = 10;
const P_RESET_GAS: usize = 11;
const P_MAX_DEPLOY: usize = 12;
const P_HARVEST_CUTOFF_KPH: usize = 13;
const P_HARVEST_RAMP_KPH: usize = 14;
const P_HARVEST_OFF_KW: usize = 15;
const P_INSTANT_CUT_KPH: usize = 16;
const P_TABLE_STEP: usize = 17;
const P_START_SPEED: usize = 18;
const P_ENTRY_SPEED: usize = 19;
const P_ENTRY_DEMAND: usize = 20;
const P_ENTRY_PREV_LEVEL: usize = 21;
const P_ENTRY_THROTTLE_TIME: usize = 22;
const P_LENGTH: usize = 23;
const P_COUNT: usize = 24;

// Indices into the `ip` (i64) parameter block.
const I_N: usize = 0;
const I_LO: usize = 1;
const I_HI: usize = 2;
const I_SETTLE: usize = 3;
const I_HAS_ENTRY: usize = 4;
const I_PRIME_START: usize = 5;
const I_N_SPLITS: usize = 6;
const I_N_TAPER: usize = 7;
const I_N_ALT: usize = 8;
const I_TABLE_LEN: usize = 9;
const I_COUNT: usize = 10;

const F_FULL_THROTTLE: u8 = 1;
const F_ON_THROTTLE: u8 = 2;
const F_IN_RESET: u8 = 4;
const F_ALT_TAPER: u8 = 8;

/// Number of values written to `out`.
const OUT_COUNT: usize = 14;

/// `fa26.curve_kw`, on points already in (kph, kW) form.
#[inline(always)]
fn curve_kw(points: &[f64], kph: f64) -> f64 {
    let m = points.len() / 2;
    if kph <= points[0] {
        return points[1];
    }
    for w in 0..m - 1 {
        let k0 = points[2 * w];
        let p0 = points[2 * w + 1];
        let k1 = points[2 * w + 2];
        let p1 = points[2 * w + 3];
        if kph <= k1 {
            return p0 + (p1 - p0) * (kph - k0) / (k1 - k0);
        }
    }
    points[2 * m - 1]
}

struct Power<'a> {
    taper: &'a [f64],
    alt: &'a [f64],
    max_deploy: f64,
    floor_kw: f64,
}

impl Power<'_> {
    /// `fa26.Powertrain.deploy_kw`.
    #[inline(always)]
    fn deploy_kw(&self, commanded: f64, speed_ms: f64, full_throttle: bool, use_alt: bool) -> f64 {
        let taper = if use_alt { self.alt } else { self.taper };
        let limit = curve_kw(taper, speed_ms * 3.6);
        // min(commanded, MAX_DEPLOY_KW)
        let mut wanted = if self.max_deploy < commanded { self.max_deploy } else { commanded };
        if full_throttle && wanted < self.floor_kw {
            wanted = self.floor_kw;
        }
        // max(0.0, min(wanted, limit))
        let least = if limit < wanted { limit } else { wanted };
        if least > 0.0 {
            least
        } else {
            0.0
        }
    }
}

/// `CarPhysics._lookup`.
#[inline(always)]
fn lookup(table: &[f64], table_step: f64, speed_ms: f64) -> f64 {
    let x = speed_ms / table_step;
    let i = x as i64; // truncates toward zero, as Python's int()
    if i < 0 {
        return table[0];
    }
    let i = i as usize;
    if i >= table.len() - 1 {
        return table[table.len() - 1];
    }
    let frac = x - i as f64;
    table[i] + (table[i + 1] - table[i]) * frac
}

/// `sim26._commands_along`: every step's (deploy, clip) command.
fn commands_along(splits: &[f64], n: usize, step: f64, length: f64) -> (Vec<f64>, Vec<f64>) {
    let count = splits.len() / 6;
    let mut order: Vec<usize> = (0..count).collect();
    // Stable, by start_m -- the same order Python's sorted() gives.
    order.sort_by(|&a, &b| {
        splits[a * 6]
            .partial_cmp(&splits[b * 6])
            .unwrap_or(std::cmp::Ordering::Equal)
    });
    let mut deploy = vec![0.0; n];
    let mut clip = vec![0.0; n];
    let mut index = 0usize;
    for i in 0..n {
        let d = (i as f64 * step) % length;
        while index < count && splits[order[index] * 6 + 3] <= d {
            index += 1;
        }
        if index >= count {
            continue;
        }
        let s = &splits[order[index] * 6..order[index] * 6 + 6];
        if s[0] > d {
            continue; // in a gap between splits
        }
        if d < s[1] {
            deploy[i] = s[4];
        } else if d < s[2] {
            // the deliberate neutral run
        } else {
            clip[i] = s[5];
        }
    }
    (deploy, clip)
}

/// Integrate one lap (or a window of one). Returns 0, or a negative number
/// when the arguments cannot be a lap, in which case nothing is written.
///
/// # Safety
/// Every pointer must be valid for the length its parameter block implies:
/// the per-step arrays for `n`, `speeds` for `hi - lo`, the lift buffers for
/// `n`, `out` for 14 values.
#[no_mangle]
pub unsafe extern "C" fn fa26_simulate(
    p: *const f64,
    ip: *const i64,
    ceiling: *const f64,
    gas_at: *const f64,
    deploy_frac: *const f64,
    harvest_frac: *const f64,
    grip_at: *const f64,
    drag_k_at: *const f64,
    cut_kph_at: *const f64,
    measured_speed: *const f64,
    flags: *const u8,
    lifting_at: *const i32,
    splits: *const f64,
    taper: *const f64,
    alt_taper: *const f64,
    force_table: *const f64,
    pow: PowFn,
    out: *mut f64,
    speeds: *mut f64,
    lift_corner: *mut i32,
    lift_kph: *mut f64,
) -> i32 {
    let p = slice::from_raw_parts(p, P_COUNT);
    let ip = slice::from_raw_parts(ip, I_COUNT);
    if ip[I_N] < 2 || ip[I_LO] < 0 || ip[I_HI] <= ip[I_LO] || ip[I_N_TAPER] < 1
        || ip[I_TABLE_LEN] < 2 || ip[I_PRIME_START] < 0 || ip[I_N_SPLITS] < 0
    {
        return -1;
    }
    let n = ip[I_N] as usize;
    let lo = ip[I_LO] as usize;
    let hi = ip[I_HI] as usize;
    let mut settle = ip[I_SETTLE] != 0;
    let has_entry = ip[I_HAS_ENTRY] != 0;
    let prime_start = ip[I_PRIME_START] as usize;

    let ceiling = slice::from_raw_parts(ceiling, n);
    let gas_at = slice::from_raw_parts(gas_at, n);
    let deploy_frac = slice::from_raw_parts(deploy_frac, n);
    let harvest_frac = slice::from_raw_parts(harvest_frac, n);
    let grip_at = slice::from_raw_parts(grip_at, n);
    let drag_k_at = slice::from_raw_parts(drag_k_at, n);
    let cut_kph_at = slice::from_raw_parts(cut_kph_at, n);
    let measured_speed = slice::from_raw_parts(measured_speed, n);
    let flags = slice::from_raw_parts(flags, n);
    let lifting_at = slice::from_raw_parts(lifting_at, n);
    let splits = slice::from_raw_parts(splits, ip[I_N_SPLITS] as usize * 6);
    let taper = slice::from_raw_parts(taper, ip[I_N_TAPER] as usize * 2);
    let alt_taper: &[f64] = if ip[I_N_ALT] > 0 {
        slice::from_raw_parts(alt_taper, ip[I_N_ALT] as usize * 2)
    } else {
        taper
    };
    let force_table = slice::from_raw_parts(force_table, ip[I_TABLE_LEN] as usize);
    let out = slice::from_raw_parts_mut(out, OUT_COUNT);
    let speeds = slice::from_raw_parts_mut(speeds, hi - lo);
    let lift_corner = slice::from_raw_parts_mut(lift_corner, n);
    let lift_kph = slice::from_raw_parts_mut(lift_kph, n);

    let step = p[P_STEP];
    let mass = p[P_MASS];
    let traction_force = p[P_TRACTION_FORCE];
    let floor_hold_s = p[P_FLOOR_HOLD_S];
    let ramp_down = p[P_RAMP_DOWN];
    let harvest_cap_kj = p[P_HARVEST_CAP];
    let store_capacity_kj = p[P_CAPACITY];
    let max_harvest = p[P_MAX_HARVEST];
    let motor_eff = p[P_MOTOR_EFF];
    let reset_gas = p[P_RESET_GAS];
    let cutoff_kph = p[P_HARVEST_CUTOFF_KPH];
    let ramp_kph = p[P_HARVEST_RAMP_KPH];
    let harvest_off_kw = p[P_HARVEST_OFF_KW];
    let instant_cut_kph = p[P_INSTANT_CUT_KPH];
    let table_step = p[P_TABLE_STEP];
    let power = Power {
        taper,
        alt: alt_taper,
        max_deploy: p[P_MAX_DEPLOY],
        floor_kw: p[P_FLOOR_KW],
    };

    let (cmd_deploy_at, cmd_clip_at) = commands_along(splits, n, step, p[P_LENGTH]);

    // Start where the driver was.
    let mut v = if ceiling[0] < p[P_START_SPEED] { ceiling[0] } else { p[P_START_SPEED] };
    let mut demand_kw = 0.0f64;
    let mut prev_level_kw = 0.0f64;
    let mut throttle_time = 0.0f64;

    // Walk the tail of the lap to establish the demand the car arrives with.
    if !has_entry {
        for i in prime_start..n {
            let f = flags[i];
            if f & F_ON_THROTTLE == 0 {
                throttle_time = 0.0;
                demand_kw = 0.0;
                prev_level_kw = 0.0;
                continue;
            }
            let ms = measured_speed[i];
            if gas_at[i] < reset_gas {
                throttle_time = 0.0;
            } else {
                // step / max(ms, 1.0)
                throttle_time += step / (if 1.0 > ms { 1.0 } else { ms });
            }
            let cmd = cmd_deploy_at[i];
            let clip = cmd_clip_at[i];
            if throttle_time <= floor_hold_s || f & F_IN_RESET != 0 || gas_at[i] < reset_gas {
                demand_kw = cmd;
            } else {
                // min(demand_kw, cmd)
                demand_kw = if cmd < demand_kw { cmd } else { demand_kw };
            }
            prev_level_kw = power.deploy_kw(
                demand_kw,
                ms,
                throttle_time <= floor_hold_s && clip <= 0.0,
                f & F_ALT_TAPER != 0,
            );
        }
    }

    let mut start_store_kj = p[P_START_STORE];
    if has_entry {
        let limit = ceiling[lo % n];
        v = if limit < p[P_ENTRY_SPEED] { limit } else { p[P_ENTRY_SPEED] };
        demand_kw = p[P_ENTRY_DEMAND];
        prev_level_kw = p[P_ENTRY_PREV_LEVEL];
        throttle_time = p[P_ENTRY_THROTTLE_TIME];
        // start_store_kj already carries the entry's charge.
        start_store_kj = p[P_START_STORE];
        settle = false;
    }
    let mut store = start_store_kj;
    let mut store_start = store;
    let mut deployed = 0.0f64;
    let mut harvested = 0.0f64;
    let mut store_min = store;
    let mut capped = false;
    let mut time_s = 0.0f64;
    let mut refused = 0.0f64;

    // Lift per sweeper, in the order each was first lifted for.
    let mut lift_sum = vec![0.0f64; n];
    let mut lift_seen = vec![false; n];
    let mut lift_order: Vec<usize> = Vec::new();
    let mut n_speeds = 0usize;

    let first_lap = if settle { 0 } else { 1 };
    for lap_index in first_lap..2 {
        let measuring = lap_index == 1;
        if measuring {
            time_s = 0.0;
            deployed = 0.0;
            harvested = 0.0;
            store_min = store;
            store_start = store;
            refused = 0.0;
            capped = false;
            for &c in &lift_order {
                lift_seen[c] = false;
                lift_sum[c] = 0.0;
            }
            lift_order.clear();
        }
        for i in lo..hi {
            let k = i % n;
            let cmd_deploy = cmd_deploy_at[k];
            let cmd_clip = cmd_clip_at[k];
            let f = flags[k];
            let full_throttle = f & F_FULL_THROTTLE != 0;
            let on_throttle = f & F_ON_THROTTLE != 0;
            let gas = gas_at[k];

            let dt_guess = step / (if v > 1.0 { v } else { 1.0 });

            let mut clip_kw = 0.0f64;
            if full_throttle && cmd_clip > 0.0 && harvested < harvest_cap_kj {
                clip_kw = if cmd_clip < max_harvest { cmd_clip } else { max_harvest };
            }

            throttle_time = if gas >= reset_gas { throttle_time + dt_guess } else { 0.0 };

            if !on_throttle {
                demand_kw = 0.0;
                prev_level_kw = 0.0;
            }

            let mut deploy_kw = 0.0f64;
            if on_throttle && store > 0.0 {
                let just_applied = throttle_time <= floor_hold_s;
                let in_reset = f & F_IN_RESET != 0;
                if just_applied || in_reset || gas < reset_gas {
                    demand_kw = cmd_deploy;
                } else if k == 0 {
                    demand_kw = if cmd_deploy < demand_kw { cmd_deploy } else { 0.0 };
                } else {
                    demand_kw = if demand_kw < cmd_deploy { demand_kw } else { cmd_deploy };
                }

                let floor_applies = just_applied && cmd_clip <= 0.0;
                let mut level =
                    power.deploy_kw(demand_kw, v, floor_applies, f & F_ALT_TAPER != 0);

                if level < prev_level_kw
                    && (v * 3.6 >= cut_kph_at[k] || cut_kph_at[k] <= instant_cut_kph)
                {
                    let floor_level = prev_level_kw - ramp_down * dt_guess;
                    if floor_level > level {
                        level = floor_level;
                    }
                }
                prev_level_kw = level;

                let target = level * deploy_frac[k];
                let drawable = store / (if dt_guess > 1e-6 { dt_guess } else { 1e-6 });
                deploy_kw = if target < drawable { target } else { drawable };
            }

            // The MGU-K cannot motor and generate at once: net the two.
            let net_kw = deploy_kw - clip_kw;
            if net_kw >= 0.0 {
                deploy_kw = net_kw;
                clip_kw = 0.0;
            } else {
                deploy_kw = 0.0;
                clip_kw = -net_kw;
            }

            let mut drive = lookup(force_table, table_step, v);
            // thrust_n: deploy_kw * 1000.0 * MOTOR_EFFICIENCY / max(speed_ms, 1.0)
            drive += deploy_kw * 1000.0 * motor_eff / (if 1.0 > v { 1.0 } else { v });
            drive -= clip_kw * 1000.0 / (if v > 1.0 { v } else { 1.0 }) / motor_eff;
            let grip_limit = traction_force * grip_at[k];
            if grip_limit < drive {
                drive = grip_limit;
            }

            let accel = (drive - drag_k_at[k] * v * v) / mass;
            let square = v * v + 2.0 * accel * step;
            let v_free = pow(if square > 1.0 { square } else { 1.0 }, 0.5);
            let next = (i + 1) % n;
            let limit = ceiling[next];
            let mut v_next = if v_free < limit { v_free } else { limit };
            let kept = if v < v_free { v } else { v_free };
            if limit < kept {
                let corner = lifting_at[next];
                if corner >= 0 {
                    let c = corner as usize;
                    if !lift_seen[c] {
                        lift_seen[c] = true;
                        lift_order.push(c);
                    }
                    lift_sum[c] += (kept - limit) * 3.6;
                }
            }
            if v_next < 1.0 {
                v_next = 1.0;
            }
            let mean_v = (v + v_next) * 0.5;
            let dt = step / (if mean_v > 1.0 { mean_v } else { 1.0 });

            let mut harvest_kw = clip_kw;
            if !full_throttle {
                // fa26.harvest_kw_at(v) * harvest_frac
                let kph = v * 3.6;
                let ceiling_kw = if kph <= cutoff_kph {
                    0.0
                } else if kph >= ramp_kph {
                    harvest_off_kw
                } else {
                    let span = ramp_kph - cutoff_kph;
                    harvest_off_kw * (kph - cutoff_kph) / span
                };
                harvest_kw = ceiling_kw * harvest_frac[k];
            }

            let mut gained = harvest_kw * dt;
            if harvested + gained > harvest_cap_kj {
                gained = harvest_cap_kj - harvested;
                if gained < 0.0 {
                    gained = 0.0;
                }
                capped = true;
            }
            let mut room = store_capacity_kj - store;
            if room < 0.0 {
                room = 0.0;
            }
            if gained > room {
                refused += gained - room;
                gained = room;
            }

            let drawn = deploy_kw * dt;
            let used = if drawn < store { drawn } else { store };
            store = store - used + gained;
            if store > store_capacity_kj {
                store = store_capacity_kj;
            }
            harvested += gained;
            deployed += used;
            if store < store_min {
                store_min = store;
            }

            v = v_next;
            time_s += dt;
            if measuring {
                speeds[i - lo] = v;
                n_speeds = i - lo + 1;
            }
        }
    }

    for (j, &c) in lift_order.iter().enumerate() {
        lift_corner[j] = c as i32;
        lift_kph[j] = lift_sum[c];
    }

    out[0] = time_s;
    out[1] = deployed;
    out[2] = harvested;
    out[3] = store;
    out[4] = store_min;
    out[5] = store_start;
    out[6] = if capped { 1.0 } else { 0.0 };
    out[7] = refused;
    out[8] = v;
    out[9] = demand_kw;
    out[10] = prev_level_kw;
    out[11] = throttle_time;
    out[12] = lift_order.len() as f64;
    out[13] = n_speeds as f64;
    0
}

/// So the Python side can refuse a library built for a different layout.
#[no_mangle]
pub extern "C" fn fa26_abi_version() -> i32 {
    1
}
