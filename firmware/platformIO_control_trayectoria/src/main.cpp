// confirmada la conexion
#include <Arduino.h>

#include "configurable_motion_data.h"
#include "hardware_config.h"
#include "communication.h"


// Limit switches variables
volatile int limit_triggered = 0;

struct Axis {
  // Fixed configuration
  int dir_pin;
  int dir_setup_us;
  int step_pin;
  unsigned int step_mask;
  hw_timer_t *timer;

  // Limit switches
  int limit_min_pin;
  int limit_max_pin;
  int positive_dir;

  // Shared with ISR
  volatile int motion_sign;
  volatile int steps_position;
  volatile int steps_remaining;
  volatile int step_state;
  volatile int stop_requested;

  // Used outside ISR
  int steps_min_range;
  int steps_max_range;
};

Axis axis_x, axis_y, axis_k;
portMUX_TYPE axes_mux = portMUX_INITIALIZER_UNLOCKED; // A mux is used because of ESP32 has two cores

static inline void IRAM_ATTR axis_isr_common(Axis &ax){
  portENTER_CRITICAL_ISR(&axes_mux);

  // Protection against a pending timer interrupt occurring immediately after axis_stop()
  if (ax.steps_remaining <= 0){
    ax.stop_requested = 0;
    timerAlarmDisable(ax.timer);
    timerStop(ax.timer);

    GPIO.out_w1tc = ax.step_mask;

    ax.step_state = 0;
    ax.steps_remaining = 0;
  }

  else{
    // Normal operation
    ax.step_state ^= 1;

    if (ax.step_state) {
      GPIO.out_w1ts = ax.step_mask; // RISING EDGE
    }
    else {
      GPIO.out_w1tc = ax.step_mask; // FALLING EDGE
      ax.steps_position += ax.motion_sign;
      ax.steps_remaining--;
    }

    // Stop requested by isr_limit() or all_axes_stop()
    if (ax.stop_requested == 1 || ax.steps_remaining <= 0) {
      ax.stop_requested = 0;
      timerAlarmDisable(ax.timer);
      timerStop(ax.timer);
      ax.steps_remaining = 0;
    }
  }

  portEXIT_CRITICAL_ISR(&axes_mux);
}

void IRAM_ATTR isr_x() {axis_isr_common(axis_x);}
void IRAM_ATTR isr_y() {axis_isr_common(axis_y);}
void IRAM_ATTR isr_k() {axis_isr_common(axis_k);}


static inline void IRAM_ATTR axis_stop(Axis &ax){
  if (ax.step_state){
    ax.stop_requested = 1;
  }
  else{
    ax.stop_requested = 0;

    timerAlarmDisable(ax.timer);
    timerStop(ax.timer);    
    ax.step_state = 0;
    ax.steps_remaining = 0;
  }
}

void IRAM_ATTR isr_limit(){
  portENTER_CRITICAL_ISR(&axes_mux);

  limit_triggered = 1;
  axis_stop(axis_x);
  axis_stop(axis_y);
  axis_stop(axis_k);

  portEXIT_CRITICAL_ISR(&axes_mux);
}


void axis_init(Axis &ax, int dir_pin, int dir_setup_us, int step_pin, int timer_id, int limit_min_pin, int limit_max_pin, int positive_dir, void (*isr)()) {
  ax.dir_pin   = dir_pin;
  ax.dir_setup_us = dir_setup_us;
  ax.step_pin  = step_pin;
  ax.step_mask = (unsigned int) 1 << step_pin;

  ax.limit_min_pin = limit_min_pin;
  ax.limit_max_pin = limit_max_pin;
  ax.positive_dir = positive_dir;

  ax.motion_sign = 0;
  ax.steps_position = 0;
  ax.steps_remaining = 0;
  ax.step_state = 0;
  ax.stop_requested = 0;

  ax.steps_min_range = 0;
  ax.steps_max_range = 0;

  pinMode(dir_pin, OUTPUT);
  pinMode(step_pin, OUTPUT);
  digitalWrite(dir_pin, LOW);
  digitalWrite(step_pin, LOW);

  ax.timer = timerBegin(timer_id, 80, true); // Default 80 MHz APB for a 1us tick
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

void axis_move_steps(Axis &ax, int direction, int steps, int steps_per_sec){
  // Verifiy if the requested axis is moving
  if (ax.steps_remaining != 0){
    return;
  }

  // Invalid movement parameters
  if (steps_per_sec <= 0 || steps <= 0) {
    return;
  }

  if (direction != 0 && direction != 1){
    return;
  }

  // Invalid direction towards a minimum limit switch
  if (!(direction == ax.positive_dir) && button_pressed(ax.limit_min_pin)){
    return;
  }

  // Invalid direction towards a maximum limit switch
  if ((direction == ax.positive_dir) && button_pressed(ax.limit_max_pin)){
    return;
  }

  int step_period_us = 1000000 / steps_per_sec;

  digitalWrite(ax.dir_pin, direction);
  delayMicroseconds(ax.dir_setup_us);

  portENTER_CRITICAL(&axes_mux);

  ax.motion_sign = (direction == ax.positive_dir) ? 1 : -1;
  ax.steps_remaining = steps;

  timerWrite(ax.timer, 0);
  timerAlarmWrite(ax.timer, step_period_us / 2, true);
  timerAlarmEnable(ax.timer);
  timerStart(ax.timer);

  portEXIT_CRITICAL(&axes_mux);
}

// Specific function
// It does not need to be atomic as long as a single movement launcher is guaranteed
int all_axes_finished(){
  return axis_x.steps_remaining == 0 && axis_y.steps_remaining == 0 && axis_k.steps_remaining == 0;
}

// Specific function
void all_axes_stop(){
  portENTER_CRITICAL(&axes_mux);

  axis_stop(axis_x);
  axis_stop(axis_y);
  axis_stop(axis_k);

  portEXIT_CRITICAL(&axes_mux);
}

void axis_calibrate_range(Axis &ax, int homing_search_steps, int homing_min_position_steps, int homing_backoff_steps, int homing_speed_steps_per_sec){
  int limit_min_pin = ax.limit_min_pin;
  int limit_max_pin = ax.limit_max_pin;
  int dir_to_min = !ax.positive_dir;
  int dir_to_max = ax.positive_dir;
  int backoff_target_position = 0;
  int backoff_steps_pending = 0;

  // Release the minimum limit switch if it is already pressed
  if (button_pressed(limit_min_pin)) {
    backoff_target_position = ax.steps_position + homing_backoff_steps;
    while (ax.steps_position < backoff_target_position){
      backoff_steps_pending = backoff_target_position - ax.steps_position;
      axis_move_steps(ax, dir_to_max, backoff_steps_pending, homing_speed_steps_per_sec);
      while (ax.steps_remaining) {vTaskDelay(pdMS_TO_TICKS(100));}
    }
  }
  vTaskDelay(pdMS_TO_TICKS(1000));
  
  // Approach the minimum limit switch
  while (!button_pressed(limit_min_pin)) {
    axis_move_steps(ax, dir_to_min, homing_search_steps, homing_speed_steps_per_sec);
    while (ax.steps_remaining) {vTaskDelay(pdMS_TO_TICKS(10));}
  }
  ax.steps_position = homing_min_position_steps;
  ax.steps_min_range = ax.steps_position;
  vTaskDelay(pdMS_TO_TICKS(1000));
  
  // Move to the maximum limit switch
  while (!button_pressed(limit_max_pin)) {
    axis_move_steps(ax, dir_to_max, homing_search_steps, homing_speed_steps_per_sec);
    while (ax.steps_remaining) {vTaskDelay(pdMS_TO_TICKS(100));}
  }
  ax.steps_max_range = ax.steps_position;
  vTaskDelay(pdMS_TO_TICKS(1000));

  // Release the maximum limit switch
  backoff_target_position = ax.steps_position - homing_backoff_steps;
  while (ax.steps_position > backoff_target_position){
    backoff_steps_pending = ax.steps_position - backoff_target_position;
    axis_move_steps(ax, dir_to_min, backoff_steps_pending, homing_speed_steps_per_sec);
    while (ax.steps_remaining) {vTaskDelay(pdMS_TO_TICKS(100));}
  }
  vTaskDelay(pdMS_TO_TICKS(1000));
}

void run_calibration(){
  // Axis Y calibration
  axis_calibrate_range(axis_y, HOMING_SEARCH_Y_STEPS, HOMING_MIN_POSITION_Y_STEPS, HOMING_BACKOFF_Y_STEPS, HOMING_SPEED_Y_STEPS_PER_SEC);
  // Axis X calibration
  axis_calibrate_range(axis_x, HOMING_SEARCH_X_STEPS, HOMING_MIN_POSITION_X_STEPS, HOMING_BACKOFF_X_STEPS, HOMING_SPEED_X_STEPS_PER_SEC);
  // Axis Knee calibration
  axis_calibrate_range(axis_k, HOMING_SEARCH_K_STEPS, HOMING_MIN_POSITION_K_STEPS, HOMING_BACKOFF_K_STEPS, HOMING_SPEED_K_STEPS_PER_SEC);
}

// Specific function
void move_axes_relative(int dx, int dy, int dk, int vx, int vy, int vk){
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
void run_trajectory(int data_length, int *dt_ms, int *dx_steps, int *dy_steps, int *dk_steps){
  int sx, sy, sk, vx, vy, vk, dirx, diry, dirk;

  Traj_state = TRAJ_ACTIVE;

  for(int i=0; i<data_length; i++){
    if(limit_triggered || dt_ms[i] <= 0){
      all_axes_stop();
      Traj_state = TRAJ_FAILED;
      return;
    }

    if (Traj_state == TRAJ_PAUSE_REQUESTED){
      Traj_state = TRAJ_PAUSED;
      while (Traj_state == TRAJ_PAUSED) {vTaskDelay(pdMS_TO_TICKS(10));}
      if (Traj_state == TRAJ_ABORT_REQUESTED){
        all_axes_stop();
        Traj_state = TRAJ_INTERRUPTED;
        return;
      }
      else if (Traj_state == TRAJ_RESUME_REQUESTED){
        Traj_state = TRAJ_ACTIVE;
      }
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
    
    vTaskDelay(pdMS_TO_TICKS(dt_ms[i]));
    while(!all_axes_finished());

  }

  Traj_state = TRAJ_FINISHED;

}


void handle_execute_calibration_command(Packet &rxPacket, Packet &txPacket){

  if (strcmp(rxPacket.command, HOME) == 0){
    run_calibration();

    strncpy(txPacket.command, READY, MAX_COMMAND_LENGTH);
    txPacket.parameters[0] = axis_x.steps_max_range;
    txPacket.parameters[1] = axis_y.steps_max_range;
    txPacket.parameters[2] = axis_k.steps_min_range;
    txPacket.parameters[3] = axis_k.steps_max_range;
    txPacket.parameter_count = 4;
  }
}

void handle_manual_relative_command(Packet &rxPacket, Packet &txPacket){
  int dx_steps = 0, dy_steps = 0, dk_steps = 0;

  if (strcmp(rxPacket.command, MANUAL) == 0){
    if (rxPacket.parameters[0]==0) dx_steps = rxPacket.parameters[1];
    else if (rxPacket.parameters[0]==1) dy_steps = rxPacket.parameters[1];
    else if (rxPacket.parameters[0]==2) dk_steps = rxPacket.parameters[1];

    if (all_axes_finished()){
      move_axes_relative(dx_steps, dy_steps, dk_steps, HOMING_SPEED_X_STEPS_PER_SEC, HOMING_SPEED_Y_STEPS_PER_SEC, HOMING_SPEED_K_STEPS_PER_SEC);
      strncpy(txPacket.command, OK, MAX_COMMAND_LENGTH);
      txPacket.parameter_count = 0;
    }
    else{
      strncpy(txPacket.command, BUSY, MAX_COMMAND_LENGTH);
      txPacket.parameter_count = 0;
    }
  }

  else if (strcmp(rxPacket.command, MANUAL_STOP) == 0){
    all_axes_stop();
    strncpy(txPacket.command, STOPPED, MAX_COMMAND_LENGTH);
    txPacket.parameter_count = 0;
  }
}

void handle_manual_absolute_command(Packet &rxPacket, Packet &txPacket){
  int dx_steps, dy_steps, dk_steps;

  if (strcmp(rxPacket.command, GOTO) == 0){

    if (all_axes_finished()){
      dx_steps = rxPacket.parameters[0] - axis_x.steps_position;
      dy_steps = rxPacket.parameters[1] - axis_y.steps_position;
      dk_steps = rxPacket.parameters[2] - axis_k.steps_position;

      move_axes_relative(dx_steps, dy_steps, dk_steps, HOMING_SPEED_X_STEPS_PER_SEC, HOMING_SPEED_Y_STEPS_PER_SEC, HOMING_SPEED_K_STEPS_PER_SEC);

      strncpy(txPacket.command, OK, MAX_COMMAND_LENGTH);
      txPacket.parameter_count = 0;
    }
    else{
      strncpy(txPacket.command, BUSY, MAX_COMMAND_LENGTH);
      txPacket.parameter_count = 0;
    }
  }

  else if (strcmp(rxPacket.command, GOTO_STOP) == 0){
    all_axes_stop();
    strncpy(txPacket.command, STOPPED, MAX_COMMAND_LENGTH);
    txPacket.parameter_count = 0;
  }

}

void handle_send_position_command(Packet &rxPacket, Packet &txPacket){
  
  if (strcmp(rxPacket.command, GET_POSITION) == 0){
    strncpy(txPacket.command, POSITION, MAX_COMMAND_LENGTH);
    txPacket.parameters[0] = axis_x.steps_position;
    txPacket.parameters[1] = axis_y.steps_position;
    txPacket.parameters[2] = axis_k.steps_position;
    txPacket.parameter_count = 3;
  }

}

void handle_receive_trajectory_command(Packet &rxPacket, Packet &txPacket){

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

void handle_execute_trajectory_command(Packet &rxPacket, Packet &txPacket){
  bool run_allowed;

  if (strcmp(rxPacket.command, RUN) == 0){
    run_allowed = Traj_state == TRAJ_IDLE ||
      Traj_state == TRAJ_FINISHED ||
      Traj_state == TRAJ_FAILED;
    
    if (!run_allowed){
      strncpy(txPacket.command, ERROR, MAX_COMMAND_LENGTH);
      txPacket.parameter_count = 0;
    }
    else{
      Traj_state = TRAJ_START_REQUESTED;
      while (Traj_state == TRAJ_START_REQUESTED) {vTaskDelay(pdMS_TO_TICKS(5));}
      strncpy(txPacket.command, RUNNING, MAX_COMMAND_LENGTH);
      txPacket.parameter_count = 0;
    }
  }

  else if (strcmp(rxPacket.command, PAUSE) == 0){
    if (Traj_state != TRAJ_ACTIVE) {
      strncpy(txPacket.command, ERROR, MAX_COMMAND_LENGTH);
      txPacket.parameter_count = 0;
    }
    else{
      Traj_state = TRAJ_PAUSE_REQUESTED;
      while (Traj_state == TRAJ_PAUSE_REQUESTED); {vTaskDelay(pdMS_TO_TICKS(5));}
      strncpy(txPacket.command, PAUSED, MAX_COMMAND_LENGTH);
      txPacket.parameter_count = 0;
    }
  }
  
  else if (strcmp(rxPacket.command, RESUME) == 0){
    Traj_state = TRAJ_RESUME_REQUESTED;
    while (Traj_state == TRAJ_RESUME_REQUESTED); {vTaskDelay(pdMS_TO_TICKS(5));}
    strncpy(txPacket.command, RUNNING, MAX_COMMAND_LENGTH);
    txPacket.parameter_count = 0;
  }

  else if (strcmp(rxPacket.command, ABORT) == 0){
    if (Traj_state != TRAJ_PAUSED) {
      strncpy(txPacket.command, ERROR, MAX_COMMAND_LENGTH);
      txPacket.parameter_count = 0;
    }
    else{
      Traj_state = TRAJ_ABORT_REQUESTED;
      while (Traj_state == TRAJ_ABORT_REQUESTED); {vTaskDelay(pdMS_TO_TICKS(5));}
      strncpy(txPacket.command, ABORTED, MAX_COMMAND_LENGTH);
      txPacket.parameter_count = 0;
    }
  }

  else if (strcmp(rxPacket.command, TRAJ_STATUS) == 0){
    if (Traj_state == TRAJ_IDLE){
      strncpy(txPacket.command, IDLE, MAX_COMMAND_LENGTH);
      txPacket.parameter_count = 0;
    }
    else if (Traj_state == TRAJ_START_REQUESTED){
      strncpy(txPacket.command, IDLE, MAX_COMMAND_LENGTH);
      txPacket.parameter_count = 0;
    }
    else if (Traj_state == TRAJ_ACTIVE){
      strncpy(txPacket.command, RUNNING, MAX_COMMAND_LENGTH);
      txPacket.parameter_count = 0;
    }
    else if (Traj_state == TRAJ_PAUSE_REQUESTED){
      strncpy(txPacket.command, RUNNING, MAX_COMMAND_LENGTH);
      txPacket.parameter_count = 0;
    }
    else if (Traj_state == TRAJ_PAUSED){
      strncpy(txPacket.command, PAUSED, MAX_COMMAND_LENGTH);
      txPacket.parameter_count = 0;
    }
    else if (Traj_state == TRAJ_ABORT_REQUESTED){
      strncpy(txPacket.command, PAUSED, MAX_COMMAND_LENGTH);
      txPacket.parameter_count = 0;
    }
    else if (Traj_state == TRAJ_ABORTED){
      strncpy(txPacket.command, ABORTED, MAX_COMMAND_LENGTH);
      txPacket.parameter_count = 0;
    }
    else if (Traj_state == TRAJ_FAILED){
      Traj_state = TRAJ_IDLE;
      strncpy(txPacket.command, FAILED, MAX_COMMAND_LENGTH);
      txPacket.parameter_count = 0;
    }
    else if (Traj_state == TRAJ_INTERRUPTED){
      Traj_state = TRAJ_IDLE;
      strncpy(txPacket.command, INTERRUPTED, MAX_COMMAND_LENGTH);
      txPacket.parameter_count = 0;
    }
    else if (Traj_state == TRAJ_FINISHED){
      Traj_state = TRAJ_IDLE;
      strncpy(txPacket.command, FINISHED, MAX_COMMAND_LENGTH);
      txPacket.parameter_count = 0;
    }
  }

}


void command_dispatcher(Packet &rxPacket, Packet &txPacket){

  if (strcmp(rxPacket.command, PING) == 0){
    strncpy(txPacket.command, PONG, MAX_COMMAND_LENGTH);
    txPacket.parameter_count = 0;
  }

  else if (strcmp(rxPacket.command, HOME) == 0){
    handle_execute_calibration_command(rxPacket, txPacket);
  }

  else if (strcmp(rxPacket.command, MANUAL) == 0){
    handle_manual_relative_command(rxPacket, txPacket);
  }

  else if (strcmp(rxPacket.command, MANUAL_STOP) == 0){
    handle_manual_relative_command(rxPacket, txPacket);
  }

  else if (strcmp(rxPacket.command, GOTO) == 0){
    handle_manual_absolute_command(rxPacket, txPacket);
  }

  else if (strcmp(rxPacket.command, GOTO_STOP) == 0){
    handle_manual_absolute_command(rxPacket, txPacket);
  }

  else if (strcmp(rxPacket.command, GET_POSITION) == 0){
    handle_send_position_command(rxPacket, txPacket);
  }

  else if (strcmp(rxPacket.command, TRAJ_BEGIN) == 0){
    handle_receive_trajectory_command(rxPacket, txPacket);
  }

  else if (strcmp(rxPacket.command, TRAJ_POINT) == 0){
    handle_receive_trajectory_command(rxPacket, txPacket);
  }

  else if (strcmp(rxPacket.command, TRAJ_END) == 0){
    handle_receive_trajectory_command(rxPacket, txPacket);
  }

  else if (strcmp(rxPacket.command, RUN) == 0){
    handle_execute_trajectory_command(rxPacket, txPacket);
  }

  else if (strcmp(rxPacket.command, PAUSE) == 0){
    handle_execute_trajectory_command(rxPacket, txPacket);
  }

  else if (strcmp(rxPacket.command, RESUME) == 0){
    handle_execute_trajectory_command(rxPacket, txPacket);
  }

  else if (strcmp(rxPacket.command, ABORT) == 0){
    handle_execute_trajectory_command(rxPacket, txPacket);
  }

  else if (strcmp(rxPacket.command, TRAJ_STATUS) == 0){
    handle_execute_trajectory_command(rxPacket, txPacket);
  }

  else{
    strncpy(txPacket.command, ERROR, MAX_COMMAND_LENGTH);
    txPacket.parameter_count = 0;
  }

}


void primary_task(void *pvParameters){
  while(1){
    communication_rx(rxPacket);
    command_dispatcher(rxPacket, txPacket);
    communication_tx(txPacket);
  }
}

void auxiliar_task(void *pvParameters){
  while(1){
    if (Traj_state == TRAJ_START_REQUESTED){
      run_trajectory(gait_data_length, gait_dt_ms, gait_dx_steps, gait_dy_steps, gait_dk_steps);
    }
    vTaskDelay(pdMS_TO_TICKS(100));
  }
}

void setup(){
  // Initialize axis X
  axis_init(axis_x, X_DIR_PIN, X_DIR_SETUP_US, X_STEP_PIN, X_TIMER_ID, LIMIT_X_MIN_PIN, LIMIT_X_MAX_PIN, X_POSITIVE_DIR, isr_x);

  // Initialize axis Y
  brake_init(Y_BRAKE_PIN, Y_BRAKE_LOCKED);
  delay(500);
  axis_init(axis_y, Y_DIR_PIN, Y_DIR_SETUP_US, Y_STEP_PIN, Y_TIMER_ID, LIMIT_Y_MIN_PIN, LIMIT_Y_MAX_PIN, Y_POSITIVE_DIR, isr_y);

  // Initialize axis Knee
  axis_init(axis_k, K_DIR_PIN, K_DIR_SETUP_US, K_STEP_PIN, K_TIMER_ID, LIMIT_K_MIN_PIN, LIMIT_K_MAX_PIN, K_POSITIVE_DIR, isr_k);

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

  // Release brake
  brake_set(Y_BRAKE_PIN, Y_BRAKE_RELEASED);
  delay(500);

  // Create primary and auxiliar tasks
  xTaskCreatePinnedToCore(primary_task ,"" , 4000, NULL, 1, NULL, 1); // Core 1 (Standard core)
  xTaskCreatePinnedToCore(auxiliar_task ,"" , 4000, NULL, 2, NULL, 0); // Core 0 (WiFi and Bluetooth core if they are active)

  // Delay
  delay(1000);
}

void loop(){

}
