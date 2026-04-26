/*

  Compiler command >>>>>

  g++ -Wall -Wextra -Og -g Redis/redis.cpp -o redis
  
  Normal command for a normal compile file.

  <<<<< Compiler command

	Sintaxis >>>>>

	char = 8 bits || Signed || -128 -> 127 ||  Can be a single character or a small number, and a byte buffer using this char[].
  In a buffer [] the parameters inside are just the quantity not the total size of types. ej buf[64] is just 64 types of something 


	Signed and unsigned = signed can be negative and unsinged can't be negative.

	uint32_t = unsigned int 32 bytes
	uint16_t = unsingned short 16 bytes
  uint8_t = unsinged char 8 bytes
	this int32_t is just used for needing an exact size of bytes, this is always 32 bits

	int32_t = int
	int16_t = short

	size_t = unsigned 0 to platform MAX
	ssize_t = signed MIN to platform MAX

	<<<<< Sintaxis

	Logic >>>>>

	const = can't be modified
	void = return nothing, modifies something
	static = limits the function to the current file kerks.

	if (eer) =  if (err != 0) // if err is something other than 0 run this block.
	if (err) = if (err == 0)
  basically if err equals something that is not 0 run this block 


	(Int) 3.14 // This cast the float and convert it into a integer (3.14 = 3)
  

  Struct 

  // Remember Heap slower and stack much faster

  Is a way to have differents objects under one name

  It dosen't use memory until a variable under its name is called

  Example :

  struct StructName {
   int health = 100;
   std::string name;

  };



  For loops 

  for ( something &s : something2)

  the first something goes thorugh every element on something2
  and if the element has a & symbol it grabs the memory location instead.


  // This is how you called it.
  StructName Sn;

  Sn.health = 80; //you can modify existing values.
  Sn.name = "kekers";

  // There is pointers aswell this how you called them on the stack 
  // This only lives on the current function called 

  StructName* Sn = &StructName //This points to the adress of the struct (0x100 example)
  Sn->health = 5;
  Sn->health // This points to the struct itself.


  // This how you called them on the heap

  StructName* Sn =  new StructName();
  Sn->health = 5;
  kek[Sn->health] = StructName; //This is how to store the pointer safely.
                                //To use on the heap you need to delete it manually
                                //The difference between the two is basically one is only in the function instance and the other survive hundreds of iteracions


 	<<<<<< Logic

	||FUNCTIONS||

	<<<<<

	MEMCPY <string.h>

	memcpy = memory copy, copies a block of raw bytes from one memory location to another 

	signature : void *memcpy(void *dest, const void *src, size_t n);

	Example :

		char buf[4]; //a byte buffer of 4 bytes
		uint32_t n = 42;

		memcpy(buf, &n, 4) // (destination, source, size of bytes)

	*This function works because bypasses types and just copys the raw bytes into that type 
	and is used in raw buffers*

	>>>>>

	read_full and write_full (read and write from the OS)

	This function are for the REDIS server project

	ssize_t read (int fd,  void *buf, size_t count) // Socket handle, Buffer pointer where the bytes are stored, How many bytes i am asking for

	Example :
		char [1024];
		ssize_t rv = read(fd, buf, 1024)

	ssize_t write (int fd, const void *buf, size_t count) // socket, buffer pointer, how many bytes to send from that buffer


	>>>>>

	<stdio.h>

	printf()

	This a C function and in theory is faster than cout but i needs to know the type of variable that is using

  Format specifiers

  % [flags] [width] [.precision] [length] type


	Variables :

	This is called the format specifiers %

	%d or %i >>> This is for signed integer (int)

	&u	 >>> unsigned integer (uint_32)

	%f	 >>> float/double (to put more decimal we use %.2f = 3.14 or %.5f = 3.14504)

	%c	 >>> char (single character)

	%s	 >>> char* (string)

	%x 	 >>> integer as hexadecimal (255 == ff)

	%p 	 >>> pointer address (ptr == 0x7ffff....)

	%zu 	 >>> size_t (print("%zu, sizeof(int)") == 4)

	%ld	 >>> long int

	Example :

		int x = 42;
		printf("your number is %d ", x);


	>>>>>>

	sizeof();

	This function returns the type with the null terminator (\0), for example :

	int x = 40;
	sizeof(x): // 4 bytes because an integer is 4 bytes

	So in practice this function completly ignores the value.

	>>>>>

	strlen()

	This is basically like the same as sizeof() but in this case is for characters for example :

	char str[] = "Hello";
	strlen(str); // It just gonna give 5 bytes of return value

	The strlen counts until the null terminator, when it reaches just returns the count of value founds.


  >>>>>>>

  data.()

  This return a raw pointer to the first element of the vector 

  is the same as &something[0]


  >>>>>>>>>

  #include <vector>

  Vector 

  well i hope i know what i am doing because i am not putting everething that a vector have

  parameters of functions 

  v.insert(end(), start, end)

  end() indicates where to start, start ? i am stuped and end 
  bro if idk how to do that last part i am retarded

  >>>>>>>>>

  Assert 

  well idk but is really simple 

  assert()

  if the logic inside is false returns it.

  >>>>>>>>>>>
  
  read and write 

  read(int fd, void *buf, size_t count)
  //which fd, buf where the kernel will put incoming bytes, the numbers of bytes


  write(int fd, const void *buf, size_t count)
  // fd, buf with the bytes to send, how many bytes

*/
