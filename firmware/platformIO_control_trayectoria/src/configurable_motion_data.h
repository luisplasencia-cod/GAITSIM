
#ifndef CONFIGURABLE_MOTION_DATA_H
#define CONFIGURABLE_MOTION_DATA_H

/*
 * Motion data is divided into two groups:
 *
 * - Runtime trajectory data: uploaded by the Raspberry Pi and expected to
 *   change during normal operation.
 * - Firmware safety and mechanical configuration: compiled into the ESP32.
 *   These values define how the hardware is calibrated and driven safely.
*/


/*************************************************************************************************************************************************/
// RUNTIME TRAJECTORY DATA
//
// A trajectory is a sequence of relative axis displacements. Each point has a
// duration in milliseconds and signed step deltas for X, Y, and K. This data is
// owned by the Raspberry Pi/user interface and is loaded into ESP32 RAM through
// the serial trajectory-transfer protocol.

#define MAX_DATA_LENGTH 500

int gait_data_length = 0;
int gait_dt_ms[MAX_DATA_LENGTH];
int gait_dx_steps[MAX_DATA_LENGTH];
int gait_dy_steps[MAX_DATA_LENGTH];
int gait_dk_steps[MAX_DATA_LENGTH];

// Reserved for future general-motion data. These arrays are not currently used
// by the firmware command flow.
int general_data_length = 0;
int general_dx_steps[MAX_DATA_LENGTH];
int general_dy_steps[MAX_DATA_LENGTH];
int general_dk_steps[MAX_DATA_LENGTH];
/*************************************************************************************************************************************************/


/*************************************************************************************************************************************************/
// FIRMWARE SAFETY AND MECHANICAL CALIBRATION CONFIGURATION
//
// These constants belong to the ESP32 because they depend on the physical
// mechanism, motor drivers, wiring, and safe homing behavior. Change them only
// after verifying the mechanical system.

// Search segment size while looking for a homing switch. Values are magnitudes
// in motor steps; the calibration routine chooses the physical direction.
#define HOMING_SEARCH_X_STEPS               800         // 2 cm
#define HOMING_SEARCH_Y_STEPS               1600        // 2 cm
#define HOMING_SEARCH_K_STEPS               300         // 2.7 deg

// Distance used to move the axis away from a detected limit switch.
// The same clearance is used to release MIN at startup and to release MAX
// after the calibration range has been measured. Values are magnitudes in
// motor steps.
#define HOMING_BACKOFF_X_STEPS              8000        // 20 cm 
#define HOMING_BACKOFF_Y_STEPS              16000       // 20 cm
#define HOMING_BACKOFF_K_STEPS              5000        // 48.6 deg

// Global reference coordinates assigned during homing. Units: motor steps.
// X and Y use their minimum switches as coordinate zero. K uses a signed
// coordinate convention, so its minimum switch is assigned a negative value.
// The K maximum reference is reserved for future validation of its calibrated
// travel range; it is not currently consumed by the firmware.
#define HOMING_MIN_POSITION_X_STEPS         0           // => 0 cm
#define HOMING_MIN_POSITION_Y_STEPS         0           // => 0 cm
#define HOMING_MIN_POSITION_K_STEPS         -5000       // => -45 deg
#define HOMING_MAX_POSITION_K_STEPS         5400        // => 48.6 deg

// Safe homing speeds. Units: motor steps per second.
#define HOMING_SPEED_X_STEPS_PER_SEC        1200        // 3 rev/s => 30 mm/s
#define HOMING_SPEED_Y_STEPS_PER_SEC        1200        // 3 rev/s => 15 mm/s
#define HOMING_SPEED_K_STEPS_PER_SEC        2000        // 2.5 rev/s => 18 deg/s
/*************************************************************************************************************************************************/


/*************************************************************************************************************************************************/
// Electrical DIR levels that produce positive motion in the global coordinate
// system. They depend on the mechanical mounting orientation, motor wiring,
// motor-driver DIR polarity/configuration, any inversion or non-inversion introduced
// by the BJT/interface board, and the positive-direction interpretation
// implemented in main.cpp. Change these values only after verifying the complete
// electrical and mechanical chain.
#define X_POSITIVE_DIR                      1
#define Y_POSITIVE_DIR                      1
#define K_POSITIVE_DIR                      0
/*************************************************************************************************************************************************/

#endif
