
#ifndef COMMUNICATION_H
#define COMMUNICATION_H

#include <Arduino.h>

#define MAX_PARAMETERS          6
#define BUFFER_SIZE             256
#define BAUD_RATE               115200
#define MAX_COMMAND_LENGTH      20
#define MAX_PARAMETER_LENGTH    8


typedef enum
{
  COMM_IDLE,
  COMM_READ_STREAM,
  COMM_END_RECEPTION
} CommunicationState;

typedef enum
{
  TRAJ_IDLE,
  TRAJ_START_REQUESTED,
  TRAJ_ACTIVE,
  TRAJ_PAUSE_REQUESTED,
  TRAJ_PAUSED,
  TRAJ_RESUME_REQUESTED,
  TRAJ_ABORT_REQUESTED,
  TRAJ_ABORTED,
  TRAJ_FINISHED,
  TRAJ_INTERRUPTED,
  TRAJ_FAILED
} TrajectoryState;

volatile TrajectoryState Traj_state = TRAJ_IDLE;

// COMMAND LIST (all strings defined here end with the null terminator \0 by default)
// Dummy command
#define NONE              "NONE"

// System commands
#define PING              "PING"
#define PONG              "PONG"

// Home execution commands
#define HOME              "HOME"
#define READY             "READY"

// Manual movement commands
#define MANUAL            "MANUAL"
#define OK                "OK"
#define BUSY              "BUSY"
#define MANUAL_STOP       "MANUAL_STOP"
#define STOPPED           "STOPPED"

// Absolute positioning commands (NOT IMPLEMENTED IN RPI SIDE)
#define GOTO              "GOTO"
//#define OK                "OK"
//#define BUSY              "BUSY"
#define GOTO_STOP         "GOTO_STOP"
//#define STOPPED           "STOPPED"

// Position requested commands
#define GET_POSITION      "GET_POSITION"
#define POSITION          "POSITION"

// Trajectory transfer commands
#define TRAJ_BEGIN        "TRAJ_BEGIN"
#define TRAJ_READY        "TRAJ_READY"
#define TRAJ_POINT        "TRAJ_POINT"
#define ACK               "ACK"
#define TRAJ_END          "TRAJ_END"
#define TRAJ_STORED       "TRAJ_STORED"

// Trajectory execution commands
#define RUN               "RUN"
#define RUNNING           "RUNNING"
#define PAUSE             "PAUSE"
#define PAUSED            "PAUSED"
#define RESUME            "RESUME"
#define ABORT             "ABORT"
#define ABORTED           "ABORTED"
#define TRAJ_STATUS       "TRAJ_STATUS"
#define IDLE              "IDLE"
#define FAILED            "FAILED"
#define INTERRUPTED       "INTERRUPTED"
#define FINISHED          "FINISHED"

// Generic error command
#define ERROR             "ERROR"


struct Packet
{
  char command[MAX_COMMAND_LENGTH];
  int parameter_count;
  int parameters[MAX_PARAMETERS];
};

Packet rxPacket;
Packet txPacket;

void communication_init(){
  Serial.setRxBufferSize(BUFFER_SIZE);
  Serial.setTxBufferSize(BUFFER_SIZE);
  Serial.begin(BAUD_RATE);
}

void communication_rx(Packet &packet){
  CommunicationState state = COMM_IDLE;
  int receiving = 1;
  int read_command = 1;

  char data[MAX_COMMAND_LENGTH];
  int data_len = 0;
  char data_part;

  // Initialize packet
  strncpy(packet.command, NONE, MAX_COMMAND_LENGTH);
  packet.parameter_count = 0;

  while(receiving){
    switch(state){

      case COMM_IDLE:
        while(!Serial.available()) {vTaskDelay(pdMS_TO_TICKS(5));}
        data_part = (char) Serial.read();

        if (data_part == '<'){
          state = COMM_READ_STREAM;
        }
        else{
          state = COMM_IDLE;
        }
      break;

      case COMM_READ_STREAM:
        while(!Serial.available()) {vTaskDelay(pdMS_TO_TICKS(5));}
        data_part = (char) Serial.read();

        if (data_part == ':' || data_part == '>'){
          data[data_len] = '\0';
          
          if (read_command){
            strncpy(packet.command, data, MAX_COMMAND_LENGTH);
            read_command = 0;
          }
          else{
            packet.parameters[packet.parameter_count] = atoi(data);
            packet.parameter_count += 1;
          }

          data_len = 0;
          state = (data_part == '>') ? COMM_END_RECEPTION : COMM_READ_STREAM;
        }
        else{
          data[data_len++] = data_part;
          state = COMM_READ_STREAM;
        }
      break;

      case COMM_END_RECEPTION:
        receiving = 0;
        state = COMM_IDLE;
      break;
    }
  }
}

void communication_tx(Packet &packet){
  //Serial.print('<');
  Serial.print(packet.command); // This does not send the null terminator \0 of the string command

  for(int i=0; i<packet.parameter_count; i++){
    Serial.print(':');
    Serial.print(packet.parameters[i]);
  }

  Serial.print('\n');
  //Serial.print('>');
}

#endif
