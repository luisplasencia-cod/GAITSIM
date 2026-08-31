// confirmada la conexion
#include <Arduino.h>

#include "hardware_config.h"
#include "configurable_motion_data.h"
#include "hardware_config.h"
#include "communication.h"


// Limit switches variables
volatile int limit_triggered = 0;
volatile int calibration = 0;

struct Axis {
  // Fixed configuration
  int dir_pin;
  int dir_setup_us;
  int step_pin;
  int step_mask;
  hw_timer_t *timer;

  // Limit switches
  int limit_min_pin;
  int limit_max_pin;
  int positive_dir;

  // Movement state
  int motion_sign = 0;
  volatile int position_steps = 0;
  volatile int steps_remaining = 0;
  volatile int step_state = 0;
};

Axis axis_x, axis_y, axis_k;

static inline void IRAM_ATTR axis_isr_common(Axis &ax){
  ax.step_state ^= 1;

  if (ax.step_state) {
    GPIO.out_w1ts = ax.step_mask; // HIGH
    ax.position_steps += ax.motion_sign;
  }
  else {
    GPIO.out_w1tc = ax.step_mask; // LOW
    ax.steps_remaining--;
  }

  if (ax.steps_remaining == 0) {
    timerAlarmDisable(ax.timer);
    timerStop(ax.timer);
  }
}

void IRAM_ATTR isr_x() {axis_isr_common(axis_x);}
void IRAM_ATTR isr_y() {axis_isr_common(axis_y);}
void IRAM_ATTR isr_k() {axis_isr_common(axis_k);}


static inline void IRAM_ATTR axis_stop(Axis &ax){
  timerAlarmDisable(ax.timer);
  timerStop(ax.timer);
  ax.step_state = 0;
  ax.steps_remaining = 0;
}

void IRAM_ATTR isr_limit(){
  if (calibration) return;

  limit_triggered = 1;
  axis_stop(axis_x);
  axis_stop(axis_y);
  axis_stop(axis_k);
}


void axis_init(Axis &ax, int dir_pin, int dir_setup_us, int step_pin, int timer_id, int limit_min_pin, int limit_max_pin, int positive_dir, void (*isr)()) {
  ax.dir_pin   = dir_pin;
  ax.dir_setup_us = dir_setup_us;
  ax.step_pin  = step_pin;
  ax.step_mask = (1 << step_pin);
  ax.limit_min_pin = limit_min_pin;
  ax.limit_max_pin = limit_max_pin;
  ax.positive_dir = positive_dir;

  pinMode(dir_pin, OUTPUT);
  pinMode(step_pin, OUTPUT);
  digitalWrite(dir_pin, LOW);
  digitalWrite(step_pin, LOW);

  ax.timer = timerBegin(timer_id, 80, true);
  timerStop(ax.timer);
  timerAttachInterrupt(ax.timer, isr, true);
  timerAlarmDisable(ax.timer);
}

// Generic function
void limit_init(int lim_pin, void (*isr)()){
  pinMode(lim_pin, INPUT);
  attachInterrupt(lim_pin, isr, INPUT_PRESSED_EDGE);
}

// Generic function
void button_init(int button_pin){
  pinMode(button_pin, INPUT);
}

// Generic function
void brake_init(int brake_pin, int brake_locked){
  pinMode(brake_pin, OUTPUT);

  // Ensure the brake remains locked at startup (default state)
  digitalWrite(brake_pin, brake_locked);
}

// Generic function
int button_pressed(int button_pin){
  return digitalRead(button_pin) == INPUT_PRESSED;
}

// Generic function
void brake_set(int brake_pin, int brake_state){
  digitalWrite(brake_pin, brake_state);
}

void axis_move_steps(Axis &ax, int direction, int steps, int steps_per_sec) {
  if (steps_per_sec == 0 || steps == 0) {
    ax.steps_remaining = 0;
    return;
  }

  int step_period_us = 1000000 / steps_per_sec;
  ax.motion_sign = (direction == ax.positive_dir) ? 1 : -1;
  ax.steps_remaining = steps;

  digitalWrite(ax.dir_pin, direction);
  delayMicroseconds(ax.dir_setup_us);

  timerWrite(ax.timer, 0);
  timerAlarmWrite(ax.timer, step_period_us / 2, true);
  timerAlarmEnable(ax.timer);
  timerStart(ax.timer);
}

// Generic function
void axis_homing_min(Axis &ax, int homing_offset_steps, int homing_speed_steps_per_sec){
  int limit_pin = ax.limit_min_pin;
  int approach_dir = !ax.positive_dir;
  int away_dir = ax.positive_dir;
  int offset_magnitude = homing_offset_steps;

  // Release the switch if it is already pressed
  if (button_pressed(limit_pin)) {
    axis_move_steps(ax, away_dir, offset_magnitude, homing_speed_steps_per_sec);
    while (ax.steps_remaining);
  }
  delay(1000);
  
  // Approach the reference switch
  while (!button_pressed(limit_pin)) {
    axis_move_steps(ax, approach_dir, 2*offset_magnitude, homing_speed_steps_per_sec);
    while (ax.steps_remaining);
  }
  delay(1000);

  // Move to the final homing position, in signed global coordinates
  axis_move_steps(ax, away_dir, offset_magnitude, homing_speed_steps_per_sec);
  while (ax.steps_remaining);
  delay(1000);
}

// Specific function
void run_homing(int homing_offset_x_steps, int homing_offset_y_steps, int homing_offset_k_steps){
  // Axis X homing
  axis_homing_min(axis_x, homing_offset_x_steps, homing_speed_x_steps_per_sec);
  // Axis Y homing
  axis_homing_min(axis_y, homing_offset_y_steps, homing_speed_y_steps_per_sec);
  // Axis Knee homing
  axis_homing_min(axis_k, homing_offset_k_steps, homing_speed_k_steps_per_sec);
}

// Specific function
void move_point_start(int dx, int dy, int dk, int vx, int vy, int vk){
  int sx, sy, sk, dirx, diry, dirk;

  sx = abs(dx);
  sy = abs(dy);
  sk = abs(dk);

  dirx = (dx >= 0) ? axis_x.positive_dir : !axis_x.positive_dir;
  diry = (dy >= 0) ? axis_y.positive_dir : !axis_y.positive_dir;
  dirk = (dk >= 0) ? axis_k.positive_dir : !axis_k.positive_dir;

  axis_move_steps(axis_x, dirx, sx, vx);
  axis_move_steps(axis_y, diry, sy, vy);
  axis_move_steps(axis_k, dirk, sk, vk);
}

// Specific function
int move_point_finished(){
  return axis_x.steps_remaining == 0 && axis_y.steps_remaining == 0 && axis_k.steps_remaining == 0;
}

// Specific function
void move_point_stop(){
  axis_stop(axis_x);
  axis_stop(axis_y);
  axis_stop(axis_k);
}

// Specific function
void run_trajectory(int data_length, int *dt_ms, int *dx_steps, int *dy_steps, int *dk_steps){
  int sx, sy, sk, vx, vy, vk, dirx, diry, dirk;

  for(int i=0; i<data_length; i++){
    if(limit_triggered){
        return;
    }
    if(dt_ms[i] <= 0){
      return;
    }

    sx = abs(dx_steps[i]);
    sy = abs(dy_steps[i]);
    sk = abs(dk_steps[i]);

    vx = sx*1000/dt_ms[i];
    vy = sy*1000 /dt_ms[i];
    vk = sk*1000/dt_ms[i];

    dirx = (dx_steps[i] >= 0) ? axis_x.positive_dir : !axis_x.positive_dir;
    diry = (dy_steps[i] >= 0) ? axis_y.positive_dir : !axis_y.positive_dir;
    dirk = (dk_steps[i] >= 0) ? axis_k.positive_dir : !axis_k.positive_dir;

    axis_move_steps(axis_x, dirx, sx, vx);
    axis_move_steps(axis_y, diry, sy, vy);
    axis_move_steps(axis_k, dirk, sk, vk);

    delay(dt_ms[i]);
    while(axis_x.steps_remaining != 0 || axis_y.steps_remaining != 0 || axis_k.steps_remaining != 0);
  }
}

void manual_control(Packet &rxPacket, Packet &txPacket){
  int dx_steps = 0, dy_steps = 0, dk_steps = 0;

  if (strcmp(rxPacket.command, MANUAL) == 0){
    if (rxPacket.parameters[0]==0) dx_steps = rxPacket.parameters[1];
    else if (rxPacket.parameters[0]==1) dy_steps = rxPacket.parameters[1];
    else if (rxPacket.parameters[0]==2) dk_steps = rxPacket.parameters[1];

    if (move_point_finished()){
      brake_set(Y_BRAKE_PIN, Y_BRAKE_RELEASED);
      delay(500);

      move_point_start(dx_steps, dy_steps, dk_steps, homing_speed_x_steps_per_sec, homing_speed_y_steps_per_sec, homing_speed_k_steps_per_sec);
      strncpy(txPacket.command, OK, MAX_COMMAND_LENGTH);
      txPacket.parameter_count = 0;
    }
    else{
      strncpy(txPacket.command, BUSY, MAX_COMMAND_LENGTH);
      txPacket.parameter_count = 0;
    }
  }

  else if (strcmp(rxPacket.command, MANUAL_STOP) == 0){
    move_point_stop();
    strncpy(txPacket.command, STOPPED, MAX_COMMAND_LENGTH);
    txPacket.parameter_count = 0;
  }
}

void manual_positioning(Packet &rxPacket, Packet &txPacket){
  int dx_steps, dy_steps, dk_steps;

  if (strcmp(rxPacket.command, GOTO) == 0){

    if (move_point_finished()){
      dx_steps = rxPacket.parameters[0] - axis_x.position_steps;
      dy_steps = rxPacket.parameters[1] - axis_y.position_steps;
      dk_steps = rxPacket.parameters[2] - axis_k.position_steps;

      brake_set(Y_BRAKE_PIN, Y_BRAKE_RELEASED);
      delay(500);
      move_point_start(dx_steps, dy_steps, dk_steps, homing_speed_x_steps_per_sec, homing_speed_y_steps_per_sec, homing_speed_k_steps_per_sec);

      strncpy(txPacket.command, OK, MAX_COMMAND_LENGTH);
      txPacket.parameter_count = 0;
    }
    else{
      strncpy(txPacket.command, BUSY, MAX_COMMAND_LENGTH);
      txPacket.parameter_count = 0;
    }
  }

  else if (strcmp(rxPacket.command, GOTO_STOP) == 0){
    move_point_stop();

    strncpy(txPacket.command, STOPPED, MAX_COMMAND_LENGTH);
    txPacket.parameter_count = 0;
  }

  else if (strcmp(rxPacket.command, POSITION) == 0){
    strncpy(txPacket.command, OK, MAX_COMMAND_LENGTH);
    txPacket.parameters[0] = axis_x.position_steps;
    txPacket.parameters[1] = axis_y.position_steps;
    txPacket.parameters[2] = axis_k.position_steps;
    txPacket.parameter_count = 0;
  }
}

void receive_traj_data(Packet &rxPacket, Packet &txPacket){

  if (strcmp(rxPacket.command, TRAJ_BEGIN) == 0){
    gait_data_length = rxPacket.parameters[0];

    strncpy(txPacket.command, TRAJ_READY, MAX_COMMAND_LENGTH);
    txPacket.parameter_count = 0;
  }

  else if (strcmp(rxPacket.command, TRAJ_POINT) == 0){
    gait_dt_ms[rxPacket.parameters[0]] = rxPacket.parameters[1];
    gait_dx_steps[rxPacket.parameters[0]] = rxPacket.parameters[2];
    gait_dy_steps[rxPacket.parameters[0]] = rxPacket.parameters[3];
    gait_dk_steps[rxPacket.parameters[0]] = rxPacket.parameters[4];

    strncpy(txPacket.command, ACK, MAX_COMMAND_LENGTH);
    txPacket.parameter_count = 1;
    txPacket.parameters[0] = rxPacket.parameters[0];
  }

  else if (strcmp(rxPacket.command, TRAJ_END) == 0){
    strncpy(txPacket.command, TRAJ_STORED, MAX_COMMAND_LENGTH);
    txPacket.parameter_count = 0;
  }

}

void execute_trajectory(Packet &rxPacket, Packet &txPacket){

  if (strcmp(rxPacket.command, RUN) == 0){
    execute_trajectory_status = run;
    //run_trajectory(gait_data_length, gait_dt_ms, gait_dx_steps, gait_dy_steps, gait_dk_steps);

    while (execute_trajectory_status!=running);
    strncpy(txPacket.command, RUNNING, MAX_COMMAND_LENGTH);
    txPacket.parameter_count = 0;
  }

  else if (strcmp(rxPacket.command, PAUSE) == 0){
    execute_trajectory_status = pause;
    while (execute_trajectory_status!=paused);
    strncpy(txPacket.command, PAUSED, MAX_COMMAND_LENGTH);
    txPacket.parameter_count = 0;
  }

  else if (strcmp(rxPacket.command, RESUME) == 0){
    execute_trajectory_status = resume;
    while (execute_trajectory_status!=running);
    strncpy(txPacket.command, RUNNING, MAX_COMMAND_LENGTH);
    txPacket.parameter_count = 0;
  }

  else if (strcmp(rxPacket.command, ABORT) == 0){
    execute_trajectory_status = abort;
    while (execute_trajectory_status!=aborted);
    strncpy(txPacket.command, ABORTED, MAX_COMMAND_LENGTH);
    txPacket.parameter_count = 0;
  }

}

void command_dispatcher(Packet &rxPacket, Packet &txPacket){

  if (strcmp(rxPacket.command, PING) == 0){
    strncpy(txPacket.command, PONG, MAX_COMMAND_LENGTH);
    txPacket.parameter_count = 0;
  }

  else if (strcmp(rxPacket.command, MANUAL) == 0){
    manual_control(rxPacket, txPacket);
  }

  else if (strcmp(rxPacket.command, MANUAL_STOP) == 0){
    manual_control(rxPacket, txPacket);
  }

  else if (strcmp(rxPacket.command, GOTO) == 0){
    manual_positioning(rxPacket, txPacket);
  }

  else if (strcmp(rxPacket.command, POSITION) == 0){
    manual_positioning(rxPacket, txPacket);
  }

  else if (strcmp(rxPacket.command, TRAJ_BEGIN) == 0){
    receive_traj_data(rxPacket, txPacket);
  }

  else if (strcmp(rxPacket.command, TRAJ_POINT) == 0){
    receive_traj_data(rxPacket, txPacket);
  }

  else if (strcmp(rxPacket.command, TRAJ_END) == 0){
    receive_traj_data(rxPacket, txPacket);
  }

}

void setup(){
  // Initialize axis X
  axis_init(axis_x, X_DIR_PIN, X_DIR_SETUP_US, X_STEP_PIN, X_TIMER_ID, LIMIT_X_MIN_PIN, LIMIT_X_MAX_PIN, x_positive_dir, isr_x);

  // Initialize axis Y
  axis_init(axis_y, Y_DIR_PIN, Y_DIR_SETUP_US, Y_STEP_PIN, Y_TIMER_ID, LIMIT_Y_MIN_PIN, LIMIT_Y_MAX_PIN, y_positive_dir, isr_y);
  brake_init(Y_BRAKE_PIN, Y_BRAKE_LOCKED);

  // Initialize axis Knee
  axis_init(axis_k, K_DIR_PIN, K_DIR_SETUP_US, K_STEP_PIN, K_TIMER_ID, LIMIT_K_MIN_PIN, LIMIT_K_MAX_PIN, k_positive_dir, isr_k);
  
  // Initialize gait start button
  button_init(START_GAIT_PIN);

  // Initialize X axis limit switches
  limit_init(LIMIT_X_MIN_PIN, isr_limit);
  limit_init(LIMIT_X_MAX_PIN, isr_limit);

  // Initialize Y axis limit switches
  limit_init(LIMIT_Y_MIN_PIN, isr_limit);
  limit_init(LIMIT_Y_MAX_PIN, isr_limit);

  // Initialize Knee axis limit switches
  limit_init(LIMIT_K_MIN_PIN, isr_limit);
  limit_init(LIMIT_K_MAX_PIN, isr_limit);
  
  // Initialize serial connection
  communication_init();

  // Delay
  delay(1000);
}


void loop(){
  communication_rx(rxPacket);
  command_dispatcher(rxPacket, txPacket);
  communication_tx(txPacket);
}
