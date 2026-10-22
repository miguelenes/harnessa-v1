/* Minimal TCP-loopback to one mounted Unix-socket relay for OpenCode. */
#define _GNU_SOURCE
#include <arpa/inet.h>
#include <errno.h>
#include <netinet/in.h>
#include <pthread.h>
#include <signal.h>
#include <stddef.h>
#include <stdlib.h>
#include <string.h>
#include <sys/socket.h>
#include <sys/un.h>
#include <unistd.h>

typedef struct {
  int source;
  int destination;
} copy_args;

static void *copy_stream(void *opaque) {
  copy_args *args = (copy_args *)opaque;
  char buffer[16384];
  for (;;) {
    ssize_t count = recv(args->source, buffer, sizeof(buffer), 0);
    if (count <= 0) break;
    ssize_t sent = 0;
    while (sent < count) {
      ssize_t result = send(args->destination, buffer + sent,
                            (size_t)(count - sent), MSG_NOSIGNAL);
      if (result <= 0) goto finished;
      sent += result;
    }
  }
finished:
  shutdown(args->destination, SHUT_WR);
  return NULL;
}

static int relay_connection(int client, const char *socket_path) {
  int upstream = socket(AF_UNIX, SOCK_STREAM, 0);
  if (upstream < 0) return 1;
  struct sockaddr_un address;
  memset(&address, 0, sizeof(address));
  address.sun_family = AF_UNIX;
  if (strlen(socket_path) >= sizeof(address.sun_path)) {
    close(upstream);
    return 1;
  }
  memcpy(address.sun_path, socket_path, strlen(socket_path) + 1);
  if (connect(upstream, (struct sockaddr *)&address, sizeof(address)) < 0) {
    close(upstream);
    return 1;
  }

  copy_args forward = {.source = client, .destination = upstream};
  copy_args reverse = {.source = upstream, .destination = client};
  pthread_t thread;
  if (pthread_create(&thread, NULL, copy_stream, &forward) != 0) {
    close(client);
    close(upstream);
    return 1;
  }
  copy_stream(&reverse);
  pthread_join(thread, NULL);
  close(client);
  close(upstream);
  return 0;
}

int main(int argc, char **argv) {
  if (argc != 3) return 2;
  const char *socket_path = argv[1];
  char *end = NULL;
  long port = strtol(argv[2], &end, 10);
  if (*end != '\0' || port < 1 || port > 65535) return 2;
  signal(SIGPIPE, SIG_IGN);
  signal(SIGCHLD, SIG_IGN);

  int listener = socket(AF_INET, SOCK_STREAM, 0);
  if (listener < 0) return 3;
  int enabled = 1;
  setsockopt(listener, SOL_SOCKET, SO_REUSEADDR, &enabled, sizeof(enabled));
  struct sockaddr_in address;
  memset(&address, 0, sizeof(address));
  address.sin_family = AF_INET;
  address.sin_port = htons((uint16_t)port);
  address.sin_addr.s_addr = htonl(INADDR_LOOPBACK);
  if (bind(listener, (struct sockaddr *)&address, sizeof(address)) < 0 ||
      listen(listener, 32) < 0) {
    close(listener);
    return 4;
  }

  for (;;) {
    int client = accept(listener, NULL, NULL);
    if (client < 0) {
      if (errno == EINTR) continue;
      break;
    }
    pid_t child = fork();
    if (child == 0) {
      close(listener);
      int result = relay_connection(client, socket_path);
      _exit(result);
    }
    close(client);
    if (child < 0) continue;
  }
  close(listener);
  return 0;
}
