
#ifndef HARDWARE_CONFIG_H
#define HARDWARE_CONFIG_H


/*************************************************************************************************************************************************/
// External board provides pull-down resistors for limit switches
#define LIMIT_X_MIN_PIN     36
#define LIMIT_X_MAX_PIN     39

#define LIMIT_Y_MIN_PIN     34
#define LIMIT_Y_MAX_PIN     35

#define LIMIT_K_MIN_PIN     32
#define LIMIT_K_MAX_PIN     33
/*************************************************************************************************************************************************/


/*************************************************************************************************************************************************/
// Limit switches and buttons are active-HIGH; external board drives LOW when inactive
#define INPUT_PRESSED       HIGH
#define INPUT_PRESSED_EDGE  RISING // Triggers when pin transitions to INPUT_PRESSED

/*************************************************************************************************************************************************/


/*************************************************************************************************************************************************/
// Axis X
#define X_DIR_PIN           23
#define X_STEP_PIN          22
#define X_TIMER_ID          0
#define X_DIR_SETUP_US      5       // Required by the horizontal motor driver datasheet
/*************************************************************************************************************************************************/


/*************************************************************************************************************************************************/
// Axis Y
#define Y_DIR_PIN           21
#define Y_STEP_PIN          19
#define Y_BRAKE_PIN         18
#define Y_TIMER_ID          1
#define Y_BRAKE_LOCKED      0       // LOW locks the brake. Power loss leaves the brake locked
#define Y_BRAKE_RELEASED    1
#define Y_DIR_SETUP_US      5       // Required by the vertical motor driver datasheet
/*************************************************************************************************************************************************/


/*************************************************************************************************************************************************/
// Axis Knee
#define K_DIR_PIN           17
#define K_STEP_PIN          16
#define K_TIMER_ID          2
#define K_DIR_SETUP_US      150     // Required by the knee motor driver datasheet
/*************************************************************************************************************************************************/


#endif
