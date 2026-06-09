 /* COPYRIGHT NOTICE OF MONITOR.C 
 * $Id$
 *
 * @brief Simple program to read/write from/to any location in memory.
 *
 * @Author Crt Valentincic <crt.valentincic@redpitaya.com>
 *         
 * (c) Red Pitaya  http://www.redpitaya.com
 *
 * This part of code is written in C programming language.
 * Please visit http://en.wikipedia.org/wiki/C_(programming_language)
 * for more details on the language used herein.
 */
/*
###############################################################################
#    pyrplockbox - DSP servo controller for quantum optics with the RedPitaya
#    Copyright (C) 2014-2016  Leonhard Neuhaus  (neuhaus@spectro.jussieu.fr)
#
#    This program is free software: you can redistribute it and/or modify
#    it under the terms of the GNU General Public License as published by
#    the Free Software Foundation, either version 3 of the License, or
#    (at your option) any later version.
#
#    This program is distributed in the hope that it will be useful,
#    but WITHOUT ANY WARRANTY; without even the implied warranty of
#    MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
#    GNU General Public License for more details.
#
#    You should have received a copy of the GNU General Public License
#    along with this program.  If not, see <http://www.gnu.org/licenses/>.
############################################################################### 
 */



 
/* 
Communication protocol for the data server:

The program is launched on the redpitaya with 

./monitor-server PORT-NUMBER, where the default port number is 2222.  

We allow for bidirectional data transfer. The client (python program) connects to the server, which in return accepts the connection. 
The client sends 8 bytes of data:
Byte 1 is interpreted as a character: 'r' for read and 'w' for write, and 'c' for close. All other messages are ignored. 
Byte 2 is reserved. 
Bytes 3+4 are interpreted as unsigned int. This number n is the amount of 4-byte-units to be read or written. Maximum is 2^16. 
Bytes 5-8 are the start address to be written to. 

If the command is read, the server will then send the requested 4*n bytes to the client. 
If the command is write, the server will wait for 4*n bytes of data from the server and write them to the designated FPGA address space. 
If the command is close, or if the connection is broken, the server program will terminate. 

After this, the server will wait for the next command. 
*/
 
 /* for now the program is utterly unoptimized... */
 
#define _GNU_SOURCE


#include <stdio.h>
#include <stdlib.h>
#include <unistd.h>
#include <string.h>
#include <errno.h>
#include <signal.h>
#include <fcntl.h>
#include <ctype.h>
#include <sys/types.h>
#include <sys/mman.h>
#include <stdint.h>
#include <sys/socket.h>
#include <netinet/in.h>
#include <netinet/tcp.h>

void error(const char *msg);

/* Robust, partial-transfer-safe socket I/O (hardening 2026-06-08).
 *
 * The original code used a single send()/recv(...,MSG_WAITALL) and treated any
 * short count as fatal (error()->exit). Under concurrent push-stream load on the
 * board a send/recv can legitimately return short or be interrupted (EINTR),
 * which killed the whole register server (single-connection, no accept loop) and
 * left the PC client spinning on a dead socket. These helpers loop over partial
 * transfers and EINTR so a busy board never desyncs the framing, and the caller
 * re-accepts instead of exiting on a genuine disconnect. */
static int send_all(int fd, const void *buf, size_t len) {
    const char *p = (const char *)buf;
    size_t sent = 0;
    while (sent < len) {
        ssize_t k = send(fd, p + sent, len - sent, 0);
        if (k > 0) { sent += (size_t)k; continue; }
        if (k < 0 && (errno == EINTR || errno == EAGAIN || errno == EWOULDBLOCK))
            continue;
        return -1;  /* peer closed / fatal socket error */
    }
    return 0;
}

/* recv exactly len bytes. returns 0 = ok, 1 = peer closed cleanly, -1 = error. */
static int recv_all(int fd, void *buf, size_t len) {
    char *p = (char *)buf;
    size_t got = 0;
    while (got < len) {
        ssize_t k = recv(fd, p + got, len - got, 0);
        if (k > 0) { got += (size_t)k; continue; }
        if (k == 0) return 1;          /* orderly peer shutdown */
        if (errno == EINTR) continue;
        return -1;                      /* error */
    }
    return 0;
}

#define FATAL do { fprintf(stderr,"Error at line %d, file %s (%d) [%s]\n", __LINE__, __FILE__, errno, strerror(errno)); \
									error("FATAL ERROR"); exit(1); } while(0)
 
//#define MAP_SIZE 4096UL
//#define MAP_SIZE 65536UL
#define MAP_SIZE 131072UL
//allowed address space: 0x40000000 to 0x41000000 has size 0x1000000 = 16MB
//  - Supports 16 modules at 1MB spacing (0x100000)
//  - Module N base address: 0x40000000 + N*0x100000 (N=0 to 15)
//#define MAP_SIZE 8388608UL
#define MAP_MASK (MAP_SIZE - 1)
#define MAX_LENGTH 65535

#define DEBUG_MONITOR 0

unsigned long read_value(unsigned long a_addr);
unsigned long* read_values(unsigned long a_addr, unsigned long* a_values_buffer, unsigned long a_len);
void write_value(unsigned long a_addr, unsigned long a_value);
void write_values(unsigned long a_addr, unsigned long* a_values, unsigned long a_len);

//FPGA memory handlers
void* map_base = (void*)(-1);
int fd = -1;

//sockets are globally defined for error handling
int sockfd;
int newsockfd;

//open and close memory mapping to FPGA registers
void open_map_base() {
    int addr = 0x40000000;
    if((fd = open("/dev/mem", O_RDWR | O_SYNC)) == -1) FATAL;
    map_base = mmap(0, MAP_SIZE, PROT_READ | PROT_WRITE, MAP_SHARED, fd, addr & ~MAP_MASK);
	if(map_base == (void *) -1) FATAL;
}

void close_map_base() {
	/*if (map_base != (void*)(-1)) {
		if(munmap(map_base, MAP_SIZE) == -1) FATAL;
		map_base = (void*)(-1);
	}
	if (fd != -1) {
		close(fd);
	}
	*/;
}
/*
//basic read and write operations
unsigned long* read_values(unsigned long a_addr, unsigned long* a_values_buffer, unsigned long a_len) {
	unsigned long* virt_addr = map_base + (a_addr & MAP_MASK);
	unsigned long i;
	printf("address to read %d",(int)a_addr);
	for (i = 0; i < a_len; i++) {;
		a_values_buffer[i] = virt_addr[i];
	}
	return a_values_buffer;
}

void write_values(unsigned long a_addr, unsigned long* a_values, unsigned long a_len) {
	void* virt_addr = map_base + (a_addr & MAP_MASK);
	unsigned long i;
	for (i = 0; i < a_len; i++) {
		((unsigned long *) virt_addr)[i] = a_values[i];
	}
}
*/

/* server process and error handling */

void error(const char *msg)
{
    perror(msg);
    close(newsockfd); 
    close(sockfd);
	//clean up the memory mapping
	close_map_base();
    exit(-1);
}

int main(int argc, char *argv[])
{
     int portno;
	 unsigned int data_length;
	 unsigned long address;
     socklen_t clilen;

     char data_buffer[8+sizeof(unsigned long)*MAX_LENGTH];
	 unsigned long * rw_buffer =(unsigned long*)&(data_buffer[8]);
	 char* buffer = (char*)&(data_buffer[0]);
     
     struct sockaddr_in serv_addr, cli_addr;
     int n;
     if (argc < 2) {
         fprintf(stderr,"ERROR, no port provided\n");
         exit(1);
     }
     /* A send() to a peer that has gone away raises SIGPIPE, whose default action
      * kills the process. Ignore it so a dropped client can never take down the
      * register server; send_all() returns -1 instead and we re-accept. */
     signal(SIGPIPE, SIG_IGN);

     sockfd = socket(AF_INET, SOCK_STREAM, 0);
     if (sockfd < 0)
        error("ERROR opening socket");
	int enable = 1;
	if (setsockopt(sockfd,SOL_SOCKET,SO_REUSEADDR,&enable,sizeof(int))<0)
		error("setsockopt(SO_REUSEADDR) failed");
     bzero((char *) &serv_addr, sizeof(serv_addr));
     portno = atoi(argv[1]);
     serv_addr.sin_family = AF_INET;
     serv_addr.sin_addr.s_addr = INADDR_ANY;
     serv_addr.sin_port = htons(portno);
     if (bind(sockfd, (struct sockaddr *) &serv_addr,
              sizeof(serv_addr)) < 0)
              error("ERROR on binding");
     listen(sockfd,5);

	//open_map_base();
	 /* Accept loop: serve one client at a time but SURVIVE disconnects / framing
	  * hiccups. A connection-level problem closes just that client socket and
	  * returns here, instead of exit()ing the whole server (the old behaviour,
	  * which forced an SSH relaunch on every blip under stream load). */
     for (;;) {
         clilen = sizeof(cli_addr);
         newsockfd = accept(sockfd,
                     (struct sockaddr *) &cli_addr,
                     &clilen);
         if (newsockfd < 0) {
             if (errno == EINTR) continue;
             error("ERROR on accept");   /* listen socket broken -> truly fatal */
         }
         /* low-latency small request/response exchanges */
         int one = 1;
         setsockopt(newsockfd, IPPROTO_TCP, TCP_NODELAY, &one, sizeof(one));

         //per-connection service loop
         for (;;) {
            //read next 8-byte header from client (robust against partial/EINTR)
            bzero(buffer,8);
            int rc = recv_all(newsockfd, buffer, 8);
            if (rc != 0) break;   //peer closed (1) or error (-1) -> re-accept

            //interpret the header
            address = ((unsigned long*)buffer)[1]; //address to be read/written
            data_length = buffer[2]+(buffer[3]<<8); //number of "unsigned long" to be read/written
            if (data_length > MAX_LENGTH)
                data_length = MAX_LENGTH;
            if (data_length == 0)
                continue;
            //test for various cases Read, Write, Close
            else if (buffer[0] == 'r') { //read from FPGA
                read_values(address, rw_buffer, data_length);
                //send header echo + data (robust against partial send/EINTR)
                if (send_all(newsockfd, data_buffer,
                             data_length*sizeof(unsigned long)+8) != 0)
                    break;   //broken connection -> re-accept
            }
            else if  (buffer[0] == 'w') { //write to FPGA
                //read new data from socket
                if (recv_all(newsockfd, rw_buffer,
                             data_length*sizeof(unsigned long)) != 0)
                    break;   //broken/short -> re-accept (no partial FPGA write)
                //write FPGA memory
                write_values(address, rw_buffer, data_length);
                if (send_all(newsockfd, buffer, 8) != 0)   //ack
                    break;
            }
            else if (buffer[0] == 'c') break; //client closed this connection
            else break; //unknown control char = desync: drop connection, re-accept
         }
         close(newsockfd);
         newsockfd = -1;
     }
	 //not reached; cleanup on fatal error happens in error()
	 close(sockfd);
	 close_map_base();
	 return 0;
}


// old version with steady reinstantiation of mmap (slow)
unsigned long* read_values(unsigned long a_addr, unsigned long* a_values_buffer, unsigned long a_len) {
    int fd = -1;
    if((fd = open("/dev/mem", O_RDWR | O_SYNC)) == -1) FATAL;
    map_base = mmap(0, MAP_SIZE, PROT_READ | PROT_WRITE, MAP_SHARED, fd, a_addr & ~MAP_MASK);
	if(map_base == (void *) -1) FATAL;
	
	void* virt_addr = map_base + (a_addr & MAP_MASK);
	unsigned long i;
	for (i = 0; i < a_len; i++) {
		a_values_buffer[i] = ((unsigned long*) virt_addr)[i];
	}

	if (map_base != (void*)(-1)) {
		if(munmap(map_base, MAP_SIZE) == -1) FATAL;
		map_base = (void*)(-1);
	}
	if (fd != -1) {
		close(fd);
	}
	return a_values_buffer;
}

void write_values(unsigned long a_addr, unsigned long* a_values, unsigned long a_len) {
    int fd = -1;
    if((fd = open("/dev/mem", O_RDWR | O_SYNC)) == -1) FATAL;
    map_base = mmap(0, MAP_SIZE, PROT_READ | PROT_WRITE, MAP_SHARED, fd, a_addr & ~MAP_MASK);
	if(map_base == (void *) -1) FATAL;
	
	void* virt_addr = map_base + (a_addr & MAP_MASK);
	unsigned long i;
	for (i = 0; i < a_len; i++) {
				((unsigned long *) virt_addr)[i] = a_values[i];
	}
	
	if (map_base != (void*)(-1)) {
		if(munmap(map_base, MAP_SIZE) == -1) FATAL;
		map_base = (void*)(-1);
	}
	if (fd != -1) {
		close(fd);
	}
}
