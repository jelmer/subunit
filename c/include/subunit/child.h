/**
 *
 *  subunit C bindings.
 *  Copyright (C) 2006  Robert Collins <robertc@robertcollins.net>
 *
 *  Licensed under either the Apache License, Version 2.0 or the BSD 3-clause
 *  license at the users choice. A copy of both licenses are available in the
 *  project source as Apache-2.0 and BSD. You may not use this file except in
 *  compliance with one of these two licences.
 *  
 *  Unless required by applicable law or agreed to in writing, software
 *  distributed under these licenses is distributed on an "AS IS" BASIS,
 *  WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
 *  See the license you chose for the specific language governing permissions
 *  and limitations under that license.
 **/


#ifdef __cplusplus
extern "C" {
#endif


/**
 * subunit_test_start:
 *
 * Report that a test is starting.
 * @name: test case name
 */
extern void subunit_test_start(char const * const name);


/**
 * subunit_test_pass:
 *
 * Report that a test has passed.
 *
 * @name: test case name
 */
extern void subunit_test_pass(char const * const name);


/**
 * subunit_test_fail:
 *
 * Report that a test has failed.
 * @name: test case name
 * @error: a string describing the error.
 */
extern void subunit_test_fail(char const * const name, char const * const error);


/**
 * subunit_test_error:
 *
 * Report that a test has errored. An error is an unintentional failure - i.e.
 * a segfault rather than a failed assertion.
 * @name: test case name
 * @error: a string describing the error.
 */
extern void subunit_test_error(char const * const name,
                               char const * const error);


#ifdef __cplusplus
}
#endif
