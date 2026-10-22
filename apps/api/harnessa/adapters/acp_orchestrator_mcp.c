#define _POSIX_C_SOURCE 200809L

#include <errno.h>
#include <poll.h>
#include <stdbool.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/socket.h>
#include <sys/un.h>
#include <unistd.h>

#define INPUT_LIMIT (1024 * 1024)
#define SOCKET_LIMIT (64 * 1024)
#define TOKEN_LIMIT 30000

typedef enum { JSON_OBJECT, JSON_ARRAY, JSON_STRING, JSON_PRIMITIVE } JsonType;

typedef struct {
    JsonType type;
    int start;
    int end;
    int parent;
    int next;
} JsonToken;

static JsonToken tokens[TOKEN_LIMIT];
static int token_count;
static const char *document;
static const char *orchestrator_socket_path;

static void skip_space(int *position) {
    while (document[*position] == ' ' || document[*position] == '\t' ||
           document[*position] == '\r' || document[*position] == '\n') {
        (*position)++;
    }
}

static int add_token(JsonType type, int start, int parent) {
    if (token_count >= TOKEN_LIMIT) return -1;
    int index = token_count++;
    tokens[index] = (JsonToken){.type = type, .start = start, .end = start,
                                .parent = parent, .next = index + 1};
    return index;
}

static int parse_value(int *position, int parent, int depth) {
    if (depth > 64) return -1;
    skip_space(position);
    char current = document[*position];
    if (current == '\0') return -1;
    if (current == '"') {
        int index = add_token(JSON_STRING, *position, parent);
        if (index < 0) return -1;
        (*position)++;
        while (document[*position] != '\0') {
            if (document[*position] == '"') {
                (*position)++;
                tokens[index].end = *position;
                tokens[index].next = token_count;
                return index;
            }
            if (document[*position] == '\\') {
                (*position)++;
                if (document[*position] == '\0') return -1;
            }
            (*position)++;
        }
        return -1;
    }
    if (current == '{' || current == '[') {
        bool object = current == '{';
        int index = add_token(object ? JSON_OBJECT : JSON_ARRAY, *position, parent);
        if (index < 0) return -1;
        (*position)++;
        skip_space(position);
        char terminator = object ? '}' : ']';
        if (document[*position] == terminator) {
            (*position)++;
            tokens[index].end = *position;
            tokens[index].next = token_count;
            return index;
        }
        while (document[*position] != '\0') {
            if (object) {
                if (document[*position] != '"' || parse_value(position, index, depth + 1) < 0)
                    return -1;
                skip_space(position);
                if (document[*position] != ':') return -1;
                (*position)++;
            }
            if (parse_value(position, index, depth + 1) < 0) return -1;
            skip_space(position);
            if (document[*position] == terminator) {
                (*position)++;
                tokens[index].end = *position;
                tokens[index].next = token_count;
                return index;
            }
            if (document[*position] != ',') return -1;
            (*position)++;
            skip_space(position);
        }
        return -1;
    }
    int index = add_token(JSON_PRIMITIVE, *position, parent);
    if (index < 0) return -1;
    while (document[*position] != '\0' && document[*position] != ',' &&
           document[*position] != ']' && document[*position] != '}' &&
           document[*position] != ' ' && document[*position] != '\t' &&
           document[*position] != '\r' && document[*position] != '\n') {
        (*position)++;
    }
    if (tokens[index].start == *position) return -1;
    tokens[index].end = *position;
    tokens[index].next = token_count;
    return index;
}

static int parse_document(const char *input) {
    document = input;
    token_count = 0;
    int position = 0;
    int root = parse_value(&position, -1, 0);
    skip_space(&position);
    return root >= 0 && document[position] == '\0' ? root : -1;
}

static bool token_equals(int index, const char *value) {
    if (index < 0 || tokens[index].type != JSON_STRING) return false;
    int length = tokens[index].end - tokens[index].start - 2;
    return length == (int)strlen(value) &&
           memcmp(document + tokens[index].start + 1, value, (size_t)length) == 0;
}

static int object_get(int object, const char *key) {
    if (object < 0 || tokens[object].type != JSON_OBJECT) return -1;
    for (int item = object + 1; item < tokens[object].next;) {
        int value = item + 1;
        if (value >= tokens[object].next) return -1;
        if (token_equals(item, key)) return value;
        item = tokens[value].next;
    }
    return -1;
}

static bool decode_hex4(const char *input, unsigned *value) {
    *value = 0;
    for (int i = 0; i < 4; i++) {
        char digit = input[i];
        unsigned nibble;
        if (digit >= '0' && digit <= '9') nibble = (unsigned)(digit - '0');
        else if (digit >= 'a' && digit <= 'f') nibble = (unsigned)(digit - 'a' + 10);
        else if (digit >= 'A' && digit <= 'F') nibble = (unsigned)(digit - 'A' + 10);
        else return false;
        *value = (*value << 4) | nibble;
    }
    return true;
}

static bool append_utf8(char *output, size_t capacity, size_t *length, unsigned codepoint) {
    unsigned char bytes[4];
    size_t count;
    if (codepoint <= 0x7f) {
        bytes[0] = (unsigned char)codepoint; count = 1;
    } else if (codepoint <= 0x7ff) {
        bytes[0] = (unsigned char)(0xc0 | (codepoint >> 6));
        bytes[1] = (unsigned char)(0x80 | (codepoint & 0x3f)); count = 2;
    } else if (codepoint <= 0xffff) {
        bytes[0] = (unsigned char)(0xe0 | (codepoint >> 12));
        bytes[1] = (unsigned char)(0x80 | ((codepoint >> 6) & 0x3f));
        bytes[2] = (unsigned char)(0x80 | (codepoint & 0x3f)); count = 3;
    } else if (codepoint <= 0x10ffff) {
        bytes[0] = (unsigned char)(0xf0 | (codepoint >> 18));
        bytes[1] = (unsigned char)(0x80 | ((codepoint >> 12) & 0x3f));
        bytes[2] = (unsigned char)(0x80 | ((codepoint >> 6) & 0x3f));
        bytes[3] = (unsigned char)(0x80 | (codepoint & 0x3f)); count = 4;
    } else return false;
    if (*length + count >= capacity) return false;
    memcpy(output + *length, bytes, count);
    *length += count;
    return true;
}

static bool decode_string(int index, char *output, size_t capacity) {
    if (index < 0 || tokens[index].type != JSON_STRING) return false;
    size_t length = 0;
    int position = tokens[index].start + 1;
    int end = tokens[index].end - 1;
    while (position < end) {
        unsigned codepoint = (unsigned char)document[position++];
        if (codepoint == '\\') {
            char escaped = document[position++];
            if (escaped == '"' || escaped == '\\' || escaped == '/') codepoint = (unsigned char)escaped;
            else if (escaped == 'b') codepoint = '\b';
            else if (escaped == 'f') codepoint = '\f';
            else if (escaped == 'n') codepoint = '\n';
            else if (escaped == 'r') codepoint = '\r';
            else if (escaped == 't') codepoint = '\t';
            else if (escaped == 'u') {
                if (!decode_hex4(document + position, &codepoint)) return false;
                position += 4;
                if (codepoint >= 0xd800 && codepoint <= 0xdbff) {
                    unsigned low;
                    if (position + 6 > end || document[position] != '\\' ||
                        document[position + 1] != 'u' ||
                        !decode_hex4(document + position + 2, &low) ||
                        low < 0xdc00 || low > 0xdfff) return false;
                    codepoint = 0x10000 + ((codepoint - 0xd800) << 10) + (low - 0xdc00);
                    position += 6;
                }
            } else return false;
        }
        if (!append_utf8(output, capacity, &length, codepoint)) return false;
    }
    output[length] = '\0';
    return true;
}

static bool write_json_string(const char *value) {
    if (putchar('"') == EOF) return false;
    for (const unsigned char *cursor = (const unsigned char *)value; *cursor; cursor++) {
        if (*cursor == '"' || *cursor == '\\') {
            if (putchar('\\') == EOF || putchar(*cursor) == EOF) return false;
        } else if (*cursor == '\n') {
            if (fputs("\\n", stdout) == EOF) return false;
        } else if (*cursor == '\r') {
            if (fputs("\\r", stdout) == EOF) return false;
        } else if (*cursor == '\t') {
            if (fputs("\\t", stdout) == EOF) return false;
        } else if (*cursor < 0x20) {
            if (printf("\\u%04x", *cursor) < 0) return false;
        } else if (putchar(*cursor) == EOF) return false;
    }
    return putchar('"') != EOF;
}

static bool write_error(const char *raw_id, int code, const char *message) {
    printf("{\"jsonrpc\":\"2.0\",\"id\":%s,\"error\":{\"code\":%d,\"message\":",
           raw_id, code);
    bool ok = write_json_string(message);
    puts("}}");
    return ok;
}

static bool write_tool_result(const char *raw_id, const char *text, bool is_error) {
    printf("{\"jsonrpc\":\"2.0\",\"id\":%s,\"result\":{\"content\":[{\"type\":\"text\",\"text\":",
           raw_id);
    bool ok = write_json_string(text);
    printf("}],\"isError\":%s}}\n", is_error ? "true" : "false");
    return ok;
}

static bool read_socket_response(const char *question_json, char *response, size_t capacity) {
    const char *socket_path = orchestrator_socket_path;
    if (socket_path == NULL || strlen(socket_path) >= sizeof(((struct sockaddr_un *)0)->sun_path))
        return false;
    int descriptor = socket(AF_UNIX, SOCK_STREAM, 0);
    if (descriptor < 0) return false;
    struct sockaddr_un address = {.sun_family = AF_UNIX};
    strcpy(address.sun_path, socket_path);
    if (connect(descriptor, (struct sockaddr *)&address, sizeof(address)) != 0) {
        close(descriptor); return false;
    }
    char request[20 * 1024];
    int request_length = snprintf(request, sizeof(request), "{\"question\":%s}\n", question_json);
    if (request_length < 0 || (size_t)request_length >= sizeof(request)) {
        close(descriptor); return false;
    }
    size_t sent = 0;
    while (sent < (size_t)request_length) {
        ssize_t count = write(descriptor, request + sent, (size_t)request_length - sent);
        if (count <= 0) { close(descriptor); return false; }
        sent += (size_t)count;
    }
    size_t received = 0;
    struct pollfd poll_descriptor = {.fd = descriptor, .events = POLLIN};
    while (received + 1 < capacity) {
        int ready = poll(&poll_descriptor, 1, 1800000);
        if (ready <= 0 || !(poll_descriptor.revents & POLLIN)) break;
        ssize_t count = read(descriptor, response + received, capacity - received - 1);
        if (count <= 0) break;
        received += (size_t)count;
        if (memchr(response, '\n', received) != NULL) {
            close(descriptor);
            response[received] = '\0';
            return true;
        }
    }
    close(descriptor);
    response[received] = '\0';
    return false;
}

static bool handle_message(char *input) {
    int root = parse_document(input);
    if (root < 0 || tokens[root].type != JSON_OBJECT) return false;
    int id = object_get(root, "id");
    int method = object_get(root, "method");
    if (id < 0) return true;
    char raw_id[128];
    int id_length = tokens[id].end - tokens[id].start;
    if (id_length <= 0 || (size_t)id_length >= sizeof(raw_id) ||
        (tokens[id].type != JSON_PRIMITIVE && tokens[id].type != JSON_STRING)) return false;
    memcpy(raw_id, document + tokens[id].start, (size_t)id_length);
    raw_id[id_length] = '\0';
    if (token_equals(method, "initialize")) {
        int params = object_get(root, "params");
        int protocol = object_get(params, "protocolVersion");
        char raw_protocol[80] = "\"2024-11-05\"";
        if (protocol >= 0 && tokens[protocol].type == JSON_STRING) {
            int length = tokens[protocol].end - tokens[protocol].start;
            if (length < (int)sizeof(raw_protocol)) {
                memcpy(raw_protocol, document + tokens[protocol].start, (size_t)length);
                raw_protocol[length] = '\0';
            }
        }
        printf("{\"jsonrpc\":\"2.0\",\"id\":%s,\"result\":{\"protocolVersion\":%s,\"capabilities\":{\"tools\":{}},\"serverInfo\":{\"name\":\"harnessa-orchestrator\",\"version\":\"1.0.0\"}}}\n",
               raw_id, raw_protocol);
        return true;
    }
    if (token_equals(method, "ping")) {
        printf("{\"jsonrpc\":\"2.0\",\"id\":%s,\"result\":{}}\n", raw_id);
        return true;
    }
    if (token_equals(method, "tools/list")) {
        printf("{\"jsonrpc\":\"2.0\",\"id\":%s,\"result\":{\"tools\":[{\"name\":\"ask_orchestrator\",\"description\":\"Persist a blocking question and wait for an authorized Harnessa operator answer.\",\"inputSchema\":{\"type\":\"object\",\"properties\":{\"question\":{\"type\":\"string\",\"minLength\":12,\"maxLength\":2000}},\"required\":[\"question\"],\"additionalProperties\":false}}]}}\n",
               raw_id);
        return true;
    }
    if (!token_equals(method, "tools/call")) return write_error(raw_id, -32601, "Method not found");
    int params = object_get(root, "params");
    int name = object_get(params, "name");
    int arguments = object_get(params, "arguments");
    int question = object_get(arguments, "question");
    if (!token_equals(name, "ask_orchestrator") || question < 0 || tokens[question].type != JSON_STRING)
        return write_error(raw_id, -32602, "Invalid ask_orchestrator arguments");
    int question_length = tokens[question].end - tokens[question].start;
    char question_json[16 * 1024];
    if (question_length < 2 || question_length >= (int)sizeof(question_json))
        return write_error(raw_id, -32602, "Clarification question exceeds the configured limit");
    memcpy(question_json, document + tokens[question].start, (size_t)question_length);
    question_json[question_length] = '\0';
    char response[SOCKET_LIMIT];
    if (!read_socket_response(question_json, response, sizeof(response)))
        return write_tool_result(raw_id, "Clarification is unavailable; stop and ask the operator.", true);
    if (parse_document(response) < 0) return write_tool_result(raw_id, "Clarification response was invalid.", true);
    int response_root = 0;
    int ok = object_get(response_root, "ok");
    if (ok < 0 || strncmp(document + tokens[ok].start, "true", 4) != 0)
        return write_tool_result(raw_id, "Clarification is unavailable; stop and ask the operator.", true);
    int answer = object_get(response_root, "answer");
    int changed = object_get(response_root, "scope_change");
    char answer_text[SOCKET_LIMIT];
    if (!decode_string(answer, answer_text, sizeof(answer_text)))
        return write_tool_result(raw_id, "Clarification response could not be decoded.", true);
    char full_text[SOCKET_LIMIT + 256];
    bool scope_change = changed >= 0 && strncmp(document + tokens[changed].start, "true", 4) == 0;
    int written = snprintf(
        full_text,
        sizeof(full_text),
        scope_change
            ? "The operator answered: %s\nHarnessa marked this as a scope change and returned the task to specification. Stop this run; do not continue implementation."
            : "The operator answered: %s\nContinue only within the current approved specification.",
        answer_text);
    if (written < 0 || (size_t)written >= sizeof(full_text))
        return write_tool_result(raw_id, "Clarification response exceeded its size limit.", true);
    return write_tool_result(raw_id, full_text, false);
}

int main(int argc, char **argv) {
    if (argc != 2 || strlen(argv[1]) >= sizeof(((struct sockaddr_un *)0)->sun_path)) return 64;
    orchestrator_socket_path = argv[1];
    char *line = NULL;
    size_t capacity = 0;
    ssize_t length;
    while ((length = getline(&line, &capacity, stdin)) >= 0) {
        if (length <= 1 || (size_t)length > INPUT_LIMIT) { free(line); return 65; }
        if (!handle_message(line)) { free(line); return 65; }
        if (fflush(stdout) != 0) { free(line); return 74; }
    }
    free(line);
    return ferror(stdin) ? 74 : 0;
}
