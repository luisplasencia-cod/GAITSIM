#include <Arduino.h>

#include "configurable_motion_data.h"
#include "hardware_config.h"


typedef enum
{
  STATE_IDLE,
  STATE_READ_STREAM,
  STATE_END_RECEPTION
} State;

typedef enum
{
  CMD_NONE = 0,
  CMD_ACK = 1,
  CMD_SET_OFFSET_STANCE = 2,
  CMD_SET_OFFSET_SWING = 3,
  CMD_SET_CONTROL_PARAMS = 4,
  CMD_RUN_X_AXIS = 5,
  CMD_RUN_Y_AXIS = 6,
  CMD_RUN_K_AXIS = 7,
  CMD_LENGTH_STANCE = 8,
  CMD_LENGTH_SWING = 9,
  CMD_DATA_STANCE = 10,
  CMD_DATA_SWING = 11,
  CMD_EXECUTE_STANCE = 12,
  CMD_EXECUTE_SWING = 13
} Command;

struct Packet
{
  int command;
  int parameter_count;
  int parameters[9];
};


// Limit switches variables
volatile int limit_triggered = 0;
volatile int calibration = 0;

// Axis X variables
hw_timer_t *x_timer = NULL;
int x_dir = 0;
volatile int x_steps_remaining = 0;
int x_steps = 0;
int x_steps_per_sec = 0;
volatile int x_step_state = 0;

// Axis Y variables
hw_timer_t *y_timer = NULL;
int y_dir = 0;
volatile int y_steps_remaining = 0;
int y_steps = 0;
int y_steps_per_sec = 0;
volatile int y_step_state = 0;

// Axis K variables
hw_timer_t *k_timer = NULL;
int k_dir = 0;
volatile int k_steps_remaining = 0;
int k_steps = 0;
int k_steps_per_sec = 0;
volatile int k_step_state = 0;


// Specific ISR function
void IRAM_ATTR isr_x(){
  x_step_state ^= 1;
  
  if(x_step_state){
    GPIO.out_w1ts = (1 << X_STEP_PIN); // HIGH
  }else{
    GPIO.out_w1tc = (1 << X_STEP_PIN); // LOW
    x_steps_remaining--;
  }

  if(x_steps_remaining==0){
    timerAlarmDisable(x_timer);
    timerStop(x_timer);
  }
}

// Specific ISR function
void IRAM_ATTR isr_y(){
  y_step_state ^= 1;
  
  if(y_step_state){
    GPIO.out_w1ts = (1 << Y_STEP_PIN); // HIGH
  }else{
    GPIO.out_w1tc = (1 << Y_STEP_PIN); // LOW
    y_steps_remaining--;
  }

  if(y_steps_remaining==0){
    timerAlarmDisable(y_timer);
    timerStop(y_timer);
  }
}

// Specific ISR function
void IRAM_ATTR isr_k(){
  k_step_state ^= 1;
  
  if(k_step_state){
    GPIO.out_w1ts = (1 << K_STEP_PIN); // HIGH
  }else{
    GPIO.out_w1tc = (1 << K_STEP_PIN); // LOW
    k_steps_remaining--;
  }

  if(k_steps_remaining==0){
    timerAlarmDisable(k_timer);
    timerStop(k_timer);
  }
}

// Specific ISR function
void IRAM_ATTR isr_limit(){
  if (calibration==1){
    return;
  }
  
  limit_triggered = 1;
  
  timerAlarmDisable(x_timer);
  timerStop(x_timer);
  x_steps_remaining = 0;

  timerAlarmDisable(y_timer);
  timerStop(y_timer);
  y_steps_remaining = 0;

  timerAlarmDisable(k_timer);
  timerStop(k_timer);
  k_steps_remaining = 0;
}


// Generic function
void limit_init(int lim_pin, void (*isr)()){
  pinMode(lim_pin, INPUT);
  attachInterrupt(lim_pin, isr, RISING);
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
void axis_init(int dir_pin, int step_pin, int timer_id, hw_timer_t **timer, void (*isr)()){
  pinMode(dir_pin, OUTPUT);
  pinMode(step_pin, OUTPUT);

  digitalWrite(dir_pin, LOW);
  digitalWrite(step_pin, LOW);

  *timer = timerBegin(timer_id, 80, true); // 1 tick = 1 us
  timerStop(*timer);
  timerAttachInterrupt(*timer, isr, true);
  timerAlarmDisable(*timer);
}

// Generic function
int button_pressed(int button_pin){
  return digitalRead(button_pin);
}

// Generic function
void brake_set(int brake_pin, int brake_state){
  digitalWrite(brake_pin, brake_state);
}

// Generic function
void axis_move_steps(int dir_pin, int direction, int dir_setup_us, hw_timer_t *timer, volatile int *steps_remaining, int steps, int steps_per_sec){
  int step_period_us;

  if(steps_per_sec == 0 || steps == 0){
    *steps_remaining = 0;
    return;
  }
  step_period_us = 1000000/steps_per_sec; // in us
  *steps_remaining = steps;

  // Configure DIR signal
  digitalWrite(dir_pin, direction);

  // Wait for DIR setup time before STEP pulses
  delayMicroseconds(dir_setup_us);
  
  timerWrite(timer, 0);
  timerAlarmWrite(timer, step_period_us/2, true);
  timerAlarmEnable(timer);
  timerStart(timer);
}

// Generic function
void axis_homing(int limit_pin, int dir_pin, int homing_direction, int dir_setup_us, hw_timer_t *timer, volatile int *steps_remaining, int homing_offset_steps, int homing_speed_steps_per_sec){
  // Release the corresponding limit switch if it is already pressed
  if(digitalRead(limit_pin) == HIGH){
    axis_move_steps(dir_pin, !homing_direction, dir_setup_us, timer, steps_remaining, homing_offset_steps, homing_speed_steps_per_sec);
    while(*steps_remaining);
  }
  delay(1000);
  
  // Try to press the switch
  while(digitalRead(limit_pin) == LOW){
    axis_move_steps(dir_pin, homing_direction, dir_setup_us, timer, steps_remaining, 2*homing_offset_steps, homing_speed_steps_per_sec);
    while(*steps_remaining);
  }
  delay(1000);

  // Move away from the switch a fixed number of steps
  calibration = 1;
  axis_move_steps(dir_pin, !homing_direction, dir_setup_us, timer, steps_remaining, homing_offset_steps, homing_speed_steps_per_sec);
  while(*steps_remaining);
  calibration = 0;
  delay(1000);
}

// Specific function
void run_homing(int homing_offset_x_steps, int homing_offset_y_steps, int homing_offset_k_steps){
  // Original calibration routine
  //int homing_dir_x, homing_dir_y, homing_dir_k;
  //int limit_x_pin, limit_y_pin, limit_k_pin;
  
  //homing_dir_x = (homing_offset_x_steps >= 0) ? X_POSITIVE_DIR : !X_POSITIVE_DIR;
  //limit_x_pin = (homing_offset_x_steps >= 0) ? LIMIT_X_MAX_PIN : LIMIT_X_MIN_PIN;

  //homing_dir_y = (homing_offset_y_steps >= 0) ? Y_POSITIVE_DIR : !Y_POSITIVE_DIR;
  //limit_y_pin = (homing_offset_y_steps >= 0) ? LIMIT_Y_MAX_PIN : LIMIT_Y_MIN_PIN;

  //homing_dir_k = (homing_offset_k_steps >= 0) ? K_POSITIVE_DIR : !K_POSITIVE_DIR;
  //limit_k_pin = (homing_offset_k_steps >= 0) ? LIMIT_K_MAX_PIN : LIMIT_K_MIN_PIN;
  
  // Axis X homing
  //axis_homing(limit_x_pin, X_DIR_PIN, homing_dir_x, X_DIR_SETUP_US, x_timer, &x_steps_remaining, abs(homing_offset_x_steps), HOMING_SPEED_X_STEPS_PER_SEC);
  // Axis Y homing
  //axis_homing(limit_y_pin, Y_DIR_PIN, homing_dir_y, Y_DIR_SETUP_US, y_timer, &y_steps_remaining, abs(homing_offset_y_steps), HOMING_SPEED_Y_STEPS_PER_SEC);
  // Axis Knee homing
  //axis_homing(limit_k_pin, KNEE_DIR_PIN, homing_dir_k, KNEE_DIR_SETUP_US, knee_timer, &knee_steps_remaining, abs(homing_offset_k_steps), HOMING_SPEED_KNEE_STEPS_PER_SEC);
  

  // Very specific calibration routine (Ymax, Kmax, K0, Ymin, Yinit, Xmax, Kmin, K0, Xmin, Xinit, Kinit)
  //        Revisar rutina alternativa  Ymax, Kmax, K0, Ymin, Yinit, Xmin, Xinit, Kinit
  //                   Otra rutina mas  Ymax, Kmax, K0, Ymin, Yinit, Xmin, Xinit, Kmax, Kinit 
  int homing_dir_k, homing_zero_k_steps;

  homing_zero_k_steps = abs(abs(homing_offset_k_steps) - abs(homing_zero_min_k_steps));
  if (abs(homing_offset_k_steps) >= abs(homing_zero_min_k_steps)) homing_dir_k = k_positive_dir;
  else homing_dir_k = !k_positive_dir;

  axis_homing(LIMIT_Y_MAX_PIN, Y_DIR_PIN, y_positive_dir, Y_DIR_SETUP_US, y_timer, &y_steps_remaining, abs(homing_offset_y_steps), homing_speed_y_steps_per_sec);
  axis_homing(LIMIT_K_MAX_PIN, K_DIR_PIN, k_positive_dir, K_DIR_SETUP_US, k_timer, &k_steps_remaining, abs(homing_zero_max_k_steps), homing_speed_k_steps_per_sec);
  axis_homing(LIMIT_Y_MIN_PIN, Y_DIR_PIN, !y_positive_dir, Y_DIR_SETUP_US, y_timer, &y_steps_remaining, abs(homing_offset_y_steps), homing_speed_y_steps_per_sec);
  axis_homing(LIMIT_X_MIN_PIN, X_DIR_PIN, !x_positive_dir, X_DIR_SETUP_US, x_timer, &x_steps_remaining, abs(homing_offset_x_steps), homing_speed_x_steps_per_sec);
  axis_move_steps(K_DIR_PIN, homing_dir_k, K_DIR_SETUP_US, k_timer, &k_steps_remaining, abs(homing_zero_k_steps), homing_speed_k_steps_per_sec);
  while(k_steps_remaining != 0);
}

// Specific function
void run_trajectory(int data_length, int *dt_ms, int *dx_steps, int *dy_steps, int *dk_steps){
  int sx, sy, sk, vx, vy, vk, dirx, diry, dirk;
  for(int i=0; i<data_length; i++){
    if(limit_triggered){
        return;
    }
    sx = abs(dx_steps[i]);
    sy = abs(dy_steps[i]);
    sk = abs(dk_steps[i]);

    vx = sx*1000/dt_ms[i];
    vy = sy*1000 /dt_ms[i];
    vk = sk*1000/dt_ms[i];

    dirx = (dx_steps[i] >= 0) ? x_positive_dir : !x_positive_dir;
    diry = (dy_steps[i] >= 0) ? y_positive_dir : !y_positive_dir;
    dirk = (dk_steps[i] >= 0) ? k_positive_dir : !k_positive_dir;

    axis_move_steps(X_DIR_PIN, dirx, X_DIR_SETUP_US, x_timer, &x_steps_remaining, sx, vx);
    axis_move_steps(Y_DIR_PIN, diry, Y_DIR_SETUP_US, y_timer, &y_steps_remaining, sy, vy);
    axis_move_steps(K_DIR_PIN, dirk, K_DIR_SETUP_US, k_timer, &k_steps_remaining, sk, vk);

    delay(dt_ms[i]);
    while(x_steps_remaining != 0 || y_steps_remaining != 0 || k_steps_remaining != 0);
  }
}


void communication_rx(Packet &packet){
  State state = STATE_IDLE;
  int receiving = 1;
  int read_command = 1;

  String data;
  data.reserve(32);
  char data_part;

  // Initialize packet
  packet.command = CMD_NONE;
  packet.parameter_count = 0;

  while(receiving){
    switch(state){

      case STATE_IDLE:
        while(!Serial.available());
        data_part = (char) Serial.read();

        if (data_part == '<'){
          state = STATE_READ_STREAM;
        }
        else{
          state = STATE_IDLE;
        }
      break;

      case STATE_READ_STREAM:
        while(!Serial.available());
        data_part = (char) Serial.read();

        if (data_part == ','){
          if (read_command){
            packet.command = (Command)data.toInt();
            read_command = 0;
          }
          else{
            packet.parameters[packet.parameter_count] = data.toInt();
            packet.parameter_count += 1;
          }

          data.remove(0);
          state = STATE_READ_STREAM;
        }

        else if(data_part == '>'){
          if (read_command){
            packet.command = (Command)data.toInt();
            read_command = 0;
          }
          else{
            packet.parameters[packet.parameter_count] = data.toInt();
            packet.parameter_count += 1;
          }
          state = STATE_END_RECEPTION;
        }

        else{
          data += data_part;
          state = STATE_READ_STREAM;
        }
      break;

      case STATE_END_RECEPTION:
        receiving = 0;
        state = STATE_IDLE;
      break;
    }
  }
}

void communication_tx(Packet &packet){
  Serial.print('<');
  Serial.print(packet.command);

  for(int i=0; i<packet.parameter_count; i++){
    Serial.print(',');
    Serial.print(packet.parameters[i]);
  }
  
  Serial.print('>');
}

void commmand_dispatcher(Packet packet){
  switch(packet.command){
    case CMD_SET_OFFSET_STANCE:
      modify_homing_values(packet);
    break;

    case CMD_SET_OFFSET_SWING:
      modify_homing_values(packet);
    break;

    case CMD_SET_CONTROL_PARAMS:
      manual_control(packet);
    break;

    case CMD_RUN_X_AXIS:
      manual_control(packet);
    break;

    case CMD_RUN_Y_AXIS:
      manual_control(packet);
    break;

    case CMD_RUN_K_AXIS:
      manual_control(packet);
    break;

    case CMD_LENGTH_STANCE:
      receive_phase_data(packet);
    break;

    case CMD_LENGTH_SWING:
      receive_phase_data(packet);
    break;

    case CMD_EXECUTE_STANCE:
      receive_phase_data(packet);
    break;

    case CMD_EXECUTE_SWING:
      receive_phase_data(packet);
    break;
  }
}



void modify_homing_values(Packet packet){

  switch(packet.command){
    case CMD_SET_OFFSET_STANCE:
      homing_offset_x_stance_steps = packet.parameters[0];
      homing_offset_y_stance_steps = packet.parameters[1];
      homing_offset_k_stance_steps = packet.parameters[2];
    break;

    case CMD_SET_OFFSET_SWING:
      homing_offset_x_stance_steps = packet.parameters[0];
      homing_offset_y_stance_steps = packet.parameters[1];
      homing_offset_k_stance_steps = packet.parameters[2];
    break;
  }

}

void manual_control(Packet packet){

  switch(packet.command){
    case CMD_SET_CONTROL_PARAMS:
      x_dir = packet.parameters[0];
      x_steps = packet.parameters[1];
      x_steps_per_sec = packet.parameters[2];
      y_dir = packet.parameters[3];
      y_steps = packet.parameters[4];
      y_steps_per_sec = packet.parameters[5];
      k_dir = packet.parameters[6];
      k_steps = packet.parameters[7];
      k_steps_per_sec = packet.parameters[8];
    break;

    case CMD_RUN_X_AXIS:
      axis_move_steps(X_DIR_PIN, x_dir, X_DIR_SETUP_US, x_timer, &x_steps_remaining, x_steps, x_steps_per_sec);
      while(x_steps_remaining != 0);
    break;

    case CMD_RUN_Y_AXIS:
      brake_set(Y_BRAKE_PIN, Y_BRAKE_RELEASED);
      axis_move_steps(Y_DIR_PIN, y_dir, Y_DIR_SETUP_US, y_timer, &y_steps_remaining, y_steps, y_steps_per_sec);
      while(y_steps_remaining != 0);
    break;

    case CMD_RUN_K_AXIS:
      axis_move_steps(K_DIR_PIN, k_dir, K_DIR_SETUP_US, k_timer, &k_steps_remaining, k_steps, k_steps_per_sec);
      while(k_steps_remaining != 0);
    break;
  }

}

void phase_homing(){
  String var_str;
  int continue_loop = 1;

  while(continue_loop){
    // receive command
    // send received

    if (var_str==String("stance_homing")){
      delay(1000);
      run_homing(homing_offset_x_stance_steps, homing_offset_y_stance_steps, homing_offset_k_stance_steps);
      delay(1000);
    }
    else if (var_str==String("swing_homing")){
      delay(1000);
      run_homing(homing_offset_x_swing_steps, homing_offset_y_swing_steps, homing_offset_k_swing_steps);
      delay(1000);
    }
    else if(var_str==String("return_menu")){
      continue_loop = 0;
    }
  }

}

void phase_execution(Packet packet){

  switch(packet.command){
    case CMD_EXECUTE_STANCE:
      delay(1000);
      limit_triggered = 0;
      run_trajectory(stance_data_length, stance_dt_ms, stance_dx_steps, stance_dy_steps, stance_dk_steps);
      delay(1000);
    break;

    case CMD_EXECUTE_SWING:
      delay(1000);
      limit_triggered = 0;
      run_trajectory(swing_data_length, swing_dt_ms, swing_dx_steps, swing_dy_steps, swing_dk_steps);
      delay(1000);
    break;
  }

}

void receive_phase_data(Packet packet){
  switch(packet.command){
    case CMD_LENGTH_STANCE:
      stance_data_length = packet.parameters[0];
    break;

    case CMD_LENGTH_SWING:
      swing_data_length = packet.parameters[0];
    break;

    case CMD_DATA_STANCE:
      stance_dt_ms[packet.parameters[0]] = packet.parameters[1];
      stance_dx_steps[packet.parameters[0]] = packet.parameters[2];
      stance_dy_steps[packet.parameters[0]] = packet.parameters[3];
      stance_dk_steps[packet.parameters[0]] = packet.parameters[4];
    break;

    case CMD_DATA_SWING:
      swing_dt_ms[packet.parameters[0]] = packet.parameters[1];
      swing_dx_steps[packet.parameters[0]] = packet.parameters[2];
      swing_dy_steps[packet.parameters[0]] = packet.parameters[3];
      swing_dk_steps[packet.parameters[0]] = packet.parameters[4];
    break;
  }

}


// Temp function
void blink_led(int times){
  for(int i = 0; i < times; i++){
    digitalWrite(2, HIGH);
    delay(1000);
    digitalWrite(2, LOW);
    delay(1000);
  }
  delay(2500);
}

void setup(){
  // Initialize axis X
  axis_init(X_DIR_PIN, X_STEP_PIN, X_TIMER_ID, &x_timer, isr_x);

  // Initialize axis Y
  axis_init(Y_DIR_PIN, Y_STEP_PIN, Y_TIMER_ID, &y_timer, isr_y);
  brake_init(Y_BRAKE_PIN, Y_BRAKE_LOCKED);

  // Initialize axis Knee
  axis_init(K_DIR_PIN, K_STEP_PIN, K_TIMER_ID, &k_timer, isr_k);
  
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
  Serial.setRxBufferSize(128);
  Serial.setTxBufferSize(128);
  Serial.begin(115200);

  // Temp line
  pinMode(2, OUTPUT);

  // Delay
  delay(1000);
}

Packet packet_rx;
Packet packet_tx;

void loop(){

  void communication_rx(Packet &packet_rx);

  // Parpadear el comando
  blink_led((int)packet_rx.command);

  // Parpadear cada parámetro
  for(int i = 0; i < packet_rx.parameter_count; i++){
    blink_led(packet_rx.parameters[i]);
  }

  packet_tx.command = CMD_ACK;
  packet_tx.parameter_count = 0;
  void communication_tx(Packet &packet_tx);
}
